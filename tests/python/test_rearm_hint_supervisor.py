"""b-244: re-arm hints must not tell operators to fight a live supervisor."""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import subprocess
import time
import sys

import pytest

from machine_isolation import AMBIENT_IDENTITY_ENV, isolated_machine_env

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
SESSION_START_HOOK = SCRIPTS / "hooks" / "goalflight-session-start-watchdog.sh"
HOOKS_MANIFEST = ROOT / "hooks" / "hooks.json"
sys.path.insert(0, str(SCRIPTS))

import goalflight_doctor as doctor  # noqa: E402
import goalflight_fleet_console as fleet  # noqa: E402
import goalflight_journal as journal  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_session_status as sessions  # noqa: E402
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


def _wait_for_supervisor_stop(
    process: subprocess.Popen[str],
    *,
    reason: str,
    timeout_s: float = 8.0,
) -> dict[str, object]:
    """Read the real supervisor stream until one child enters stopped_reason."""
    assert process.stdout is not None
    deadline = time.monotonic() + timeout_s
    observed: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        if process.poll() is not None:
            diagnostic = process.stderr.read() if process.stderr is not None else ""
            raise AssertionError(
                f"supervisor exited {process.returncode}: {diagnostic}"
            )
        readable, _writable, _errored = select.select(
            [process.stdout],
            [],
            [],
            max(0.0, min(0.1, deadline - time.monotonic())),
        )
        if not readable:
            continue
        line = process.stdout.readline()
        if not line.startswith("{"):
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            continue
        observed.append(record)
        if record.get("type") == "stop" and record.get("reason") == reason:
            return record
    raise AssertionError(f"supervisor did not stop a slot: {observed}")


def _start_in_flight_attempt(
    project: Path,
    lease: journal.LeaseIdentity,
    dispatch_id: str,
) -> None:
    authority = journal.open_or_create_journal(project)
    prepared = authority.prepare_attempt(
        dispatch_id,
        owner_controller_label=lease.label,
        owner_session_nonce=lease.nonce,
    )
    assert prepared.committed and prepared.value is not None
    attempt = prepared.value
    starting = authority.start_attempt(attempt.attempt_id, attempt.launch_token)
    assert starting.committed and starting.value is not None
    started = starting.value
    running = authority.mark_attempt_running(
        started.attempt_id,
        started.launch_token,
        launch_epoch=started.launch_epoch,
        worker_instance={"pid": os.getpid(), "source": "stopped-slot-test"},
    )
    assert running.committed


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


def _session_start_embedded_python() -> str:
    text = SESSION_START_HOOK.read_text(encoding="utf-8")
    marker = "python3 - <<'PY' 2>/dev/null || true\n"
    start = text.index(marker) + len(marker)
    end = text.index("\nPY\n", start)
    return text[start:end]


def _invoke_session_start_hook(
    project: Path,
    env: dict[str, str],
    *,
    source: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", str(SESSION_START_HOOK)],
        cwd=project,
        env=env,
        input=json.dumps(
            {
                "hook_event_name": "SessionStart",
                "source": source,
                "cwd": str(project),
            }
        ),
        capture_output=True,
        text=True,
        timeout=12,
        check=True,
        start_new_session=True,
    )


def _run_session_start_hook(
    project: Path,
    env: dict[str, str],
    *,
    source: str,
) -> str:
    completed = _invoke_session_start_hook(project, env, source=source)
    assert completed.stderr == ""
    payload = json.loads(completed.stdout)
    return str(payload["hookSpecificOutput"]["additionalContext"])


def _session_start_test_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    label: str,
) -> dict[str, str]:
    for key in AMBIENT_IDENTITY_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("GOALFLIGHT_WAKE_LEDGER", raising=False)
    assignments = isolated_machine_env(tmp_path)
    assignments.update(
        {
            "GOALFLIGHT_CONTROLLER_LABEL": label,
            "GOALFLIGHT_PROCESS_ROLE": "controller",
            "GOALFLIGHT_TEST_MODE": "1",
            "GOALFLIGHT_WATCHDOG_RECENT_SECONDS": "0",
        }
    )
    for key, value in assignments.items():
        monkeypatch.setenv(key, value)
    return dict(os.environ)


def _ps_listing_env(
    tmp_path: Path,
    env: dict[str, str],
    *,
    name: str,
    rows: list[str],
) -> dict[str, str]:
    shim_dir = tmp_path / name
    shim_dir.mkdir()
    ps_shim = shim_dir / "ps"
    body = "#!/bin/sh\n"
    body += "".join(
        f"printf '%s\\n' {shlex.quote(row)}\n" for row in rows
    )
    body += "exit 0\n"
    ps_shim.write_text(body, encoding="utf-8")
    ps_shim.chmod(0o755)
    return {**env, "PATH": f"{shim_dir}:{env.get('PATH', '')}"}


def test_session_start_hook_startup_and_resume_use_live_supervisor_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hooks = json.loads(HOOKS_MANIFEST.read_text(encoding="utf-8"))["hooks"]
    session_start = hooks["SessionStart"]
    assert len(session_start) == 1
    assert session_start[0]["matcher"] == "startup|resume"
    assert session_start[0]["hooks"] == [
        {
            "type": "command",
            "command": (
                "${CLAUDE_PLUGIN_ROOT}/scripts/hooks/"
                "goalflight-session-start-watchdog.sh"
            ),
            "timeout": 5,
        }
    ]

    project = tmp_path / "hook project"
    project.mkdir()
    (project / "SKILL.md").write_text("hook integration facade\n", encoding="utf-8")
    (project / "scripts").symlink_to(SCRIPTS, target_is_directory=True)

    for key in AMBIENT_IDENTITY_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("GOALFLIGHT_WAKE_LEDGER", raising=False)
    assignments = isolated_machine_env(tmp_path / "hook-machine")
    assignments.update(
        {
            "GOALFLIGHT_ROOT": str(ROOT),
            "GOALFLIGHT_CONTROLLER_LABEL": "hook-controller",
            "GOALFLIGHT_PROCESS_ROLE": "controller",
            "GOALFLIGHT_TEST_MODE": "1",
            "GOALFLIGHT_WAKE_ENTRY_POLL_S": "0",
            "GOALFLIGHT_WATCHDOG_RECENT_SECONDS": "0",
        }
    )
    for key, value in assignments.items():
        monkeypatch.setenv(key, value)
    env = dict(os.environ)
    authority = journal.open_or_create_journal(project)
    assert authority.prepare_attempt("hook-live-supervisor").committed

    # First entry establishes the real ancestry-bound controller generation.
    absent_env = _ps_listing_env(
        tmp_path,
        env,
        name="probe-absent-startup",
        rows=[],
    )
    initial_context = _run_session_start_hook(project, absent_env, source="startup")
    lease = authority.active_lease("hook-controller")
    assert lease is not None, initial_context
    absent_command = wake.listener_start_command(
        project,
        controller_label=lease.label,
    )
    assert '"listener_depth": {' in initial_context
    assert absent_command in initial_context, initial_context
    env["GOALFLIGHT_CONTROLLER_SESSION_ID"] = lease.nonce
    env["GOALFLIGHT_CONTROLLER_LEASE_NONCE"] = lease.nonce

    supervise_parts = shlex.split(
        wake.coverage_supervise_command(
            project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
        )
    )
    supervise_parts[0] = sys.executable
    supervisor = subprocess.Popen(
        supervise_parts,
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(0.2)
        if supervisor.poll() is not None:
            diagnostic = (
                supervisor.stderr.read() if supervisor.stderr is not None else ""
            )
            raise AssertionError(
                f"supervisor exited {supervisor.returncode}: {diagnostic}"
            )
        running_shim_dir = tmp_path / "probe-running"
        running_shim_dir.mkdir()
        running_ps = running_shim_dir / "ps"
        process_row = f"{supervisor.pid} {shlex.join(supervise_parts)}"
        running_ps.write_text(
            "#!/bin/sh\nprintf '%s\\n' " + shlex.quote(process_row) + "\n",
            encoding="utf-8",
        )
        running_ps.chmod(0o755)
        running_env = {
            **env,
            "PATH": f"{running_shim_dir}:{env.get('PATH', '')}",
        }
        component_commands = _component_commands(project, lease)
        for source in ("startup", "resume"):
            context = _run_session_start_hook(project, running_env, source=source)
            assert supervisor.poll() is None
            assert '"wake_supervisor": "running"' in context
            assert '"listener_depth"' not in context
            assert '"live":' not in context
            assert '"target":' not in context
            assert '"missing":' not in context
            assert "no controller wake action is required" in context
            assert "Restart the supervisor" not in context
            assert "ARM THE EVENT WAKE FIRST" not in context
            assert "goalflight_messages.py listen" not in context
            assert "goalflight_messages.py follow" not in context
            for component_command in component_commands:
                assert component_command not in context

        # Real supervisor, real spaced root, and a ps-shaped argv with its
        # quoting removed. The hook must carry UNKNOWN through the production
        # detector instead of exposing a component command.
        raw_row = f"{supervisor.pid} {' '.join(supervise_parts)}"
        spaced_env = _ps_listing_env(
            tmp_path,
            env,
            name="probe-spaced-root",
            rows=[raw_row],
        )
        for source in ("startup", "resume"):
            context = _run_session_start_hook(project, spaced_env, source=source)
            assert supervisor.poll() is None
            assert '"supervisor": "unknown"' in context
            assert '"live":' not in context
            assert '"target":' not in context
            assert '"missing":' not in context
            assert "could not tell whether `supervise`" in context
            assert "RESOLVE EVENT WAKE OWNERSHIP FIRST" in context
            for component_command in component_commands:
                assert component_command not in context

        # Keep the real supervisor alive while only the hook/status subprocess
        # loses process-table access. This exercises UNKNOWN at the producer,
        # not by supplying a precomputed supervisor state.
        shim_dir = tmp_path / "probe-unavailable"
        shim_dir.mkdir()
        ps_shim = shim_dir / "ps"
        ps_shim.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        ps_shim.chmod(0o755)
        unknown_env = {**env, "PATH": f"{shim_dir}:{env.get('PATH', '')}"}
        for source in ("startup", "resume"):
            context = _run_session_start_hook(project, unknown_env, source=source)
            assert supervisor.poll() is None
            assert '"supervisor": "unknown"' in context
            assert '"live":' not in context
            assert '"target":' not in context
            assert '"missing":' not in context
            assert "could not tell whether `supervise`" in context
            assert "RESOLVE EVENT WAKE OWNERSHIP FIRST" in context
            assert "goalflight_messages.py listen" not in context
            assert "goalflight_messages.py follow" not in context
            for component_command in component_commands:
                assert component_command not in context

    finally:
        if supervisor.poll() is None:
            supervisor.terminate()
            try:
                supervisor.wait(timeout=5)
            except subprocess.TimeoutExpired:
                supervisor.kill()
                supervisor.wait(timeout=5)
        if supervisor.stdout is not None:
            supervisor.stdout.close()
        if supervisor.stderr is not None:
            supervisor.stderr.close()


def test_session_start_journal_activity_bounds_open_retry_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SessionStart journal peek must not inherit the 75s open budget."""
    recorded: dict[str, object] = {}

    def fake_open_reader(cls, project_root, **kwargs):  # type: ignore[no-untyped-def]
        recorded["root"] = project_root
        recorded["kwargs"] = kwargs
        raise journal.JournalBusy("seam")

    monkeypatch.setattr(
        journal.Journal, "open_reader", classmethod(fake_open_reader)
    )
    code = _session_start_embedded_python().replace(
        "try:\n    main()\nexcept Exception:\n    pass",
        "",
        1,
    )
    ns: dict[str, object] = {}
    exec(compile(code, str(SESSION_START_HOOK), "exec"), ns)
    journal_activity = ns["journal_activity"]
    assert callable(journal_activity)
    project = tmp_path / "project"
    project.mkdir()
    (project / "SKILL.md").write_text("hook open-budget seam\n", encoding="utf-8")
    assert journal_activity(str(ROOT), str(project)) == "unknown"  # type: ignore[operator]
    kwargs = recorded["kwargs"]
    assert isinstance(kwargs, dict)
    assert "open_retry_budget_s" in kwargs
    open_budget = float(kwargs["open_retry_budget_s"])
    assert 0 <= open_budget <= 1.0
    assert open_budget < journal.JOURNAL_OPEN_RETRY_BUDGET_S
    retry_budget = float(
        kwargs.get("retry_budget_s", journal.JOURNAL_READER_RETRY_BUDGET_S)
    )
    assert retry_budget <= journal.JOURNAL_READER_RETRY_BUDGET_S
    assert float(kwargs["transaction_budget_s"]) <= 1.0


@pytest.mark.parametrize(
    ("claim_result", "expected_arm", "expected_fragment"),
    [
        pytest.param(
            {
                "claimed": True,
                "session": {"lease_nonce": "test-nonce"},
                "listener_depth": {
                    "supervisor": "absent",
                    "command": "test-supervise-command",
                },
            },
            True,
            "ARM THE EVENT WAKE FIRST",
            id="claimed",
        ),
        pytest.param(
            {"claimed": False, "reason": "label_in_use"},
            False,
            "claim was refused with reason `label_in_use`",
            id="refused",
        ),
        pytest.param(
            {},
            False,
            (
                "claim state unknown: re-run "
                "`goalflight_session_status.py --controller-startup` before arming"
            ),
            id="unknown",
        ),
    ],
)
def test_session_start_claim_outcome_controls_arm_preamble(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    claim_result: dict[str, object],
    expected_arm: bool,
    expected_fragment: str,
) -> None:
    project = tmp_path / "claim-outcome-project"
    project.mkdir()
    (project / "SKILL.md").write_text("hook claim outcome\n", encoding="utf-8")
    (project / "scripts").symlink_to(SCRIPTS, target_is_directory=True)
    code = _session_start_embedded_python().replace(
        "try:\n    main()\nexcept Exception:\n    pass",
        "",
        1,
    )
    ns: dict[str, object] = {}
    exec(compile(code, str(SESSION_START_HOOK), "exec"), ns)
    monkeypatch.setenv(
        "GOALFLIGHT_HOOK_INPUT",
        json.dumps(
            {
                "hook_event_name": "SessionStart",
                "source": "startup",
                "cwd": str(project),
            }
        ),
    )
    monkeypatch.setenv("GOALFLIGHT_PLUGIN_ROOT", str(ROOT))
    ns["has_recent_resume_note"] = lambda: True
    ns["claim_controller_entry"] = lambda *_args, **_kwargs: claim_result
    wake_instruction_calls: list[dict[str, object]] = []

    def wake_instruction(_repo_root: str, result: dict[str, object]) -> str:
        wake_instruction_calls.append(result)
        return "Claimed-controller wake details."

    ns["controller_wake_instruction"] = wake_instruction
    main = ns["main"]
    assert callable(main)
    main()

    payload = json.loads(capsys.readouterr().out)
    context = str(payload["hookSpecificOutput"]["additionalContext"])
    assert expected_fragment in context
    assert ("ARM THE EVENT WAKE FIRST" in context) is expected_arm
    assert len(wake_instruction_calls) == (1 if expected_arm else 0)
    if claim_result.get("reason") == "label_in_use":
        assert "adopt a dead or same-session holder" in context
        assert (
            "goalflight_session_status.py --controller-startup "
            "--controller-pid-from-ancestry --takeover"
        ) in context


def test_session_start_probe_failure_emits_without_claiming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "probe-failure-project"
    project.mkdir()
    (project / "SKILL.md").write_text("hook probe failure\n", encoding="utf-8")
    (project / "scripts").symlink_to(SCRIPTS, target_is_directory=True)
    status_script = tmp_path / "failed-status.py"
    status_script.write_text("import time\ntime.sleep(4)\n", encoding="utf-8")

    env = _session_start_test_env(
        tmp_path / "probe-failure-machine",
        monkeypatch,
        label="probe-failure-controller",
    )
    env["GOALFLIGHT_WATCHDOG_STATUS_SCRIPT"] = str(status_script)

    completed = _invoke_session_start_hook(project, env, source="startup")
    assert completed.stderr == ""
    payload = json.loads(completed.stdout)
    context = str(payload["hookSpecificOutput"]["additionalContext"])
    assert "could not determine Goal Flight activity" in context
    assert "ARM THE EVENT WAKE FIRST" not in context
    assert (
        "claim state unknown: re-run "
        "`goalflight_session_status.py --controller-startup` before arming"
    ) in context
    assert "CONTINUE IN-SKILL" in context
    assert "did not claim a controller lease" in context
    authority = journal.open_or_create_journal(project)
    assert authority.active_lease("probe-failure-controller") is None


def test_session_start_probe_failures_share_hook_wall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "shared-wall-project"
    project.mkdir()
    (project / "SKILL.md").write_text("hook shared wall\n", encoding="utf-8")
    (project / "scripts").symlink_to(SCRIPTS, target_is_directory=True)
    code = _session_start_embedded_python().replace(
        "try:\n    main()\nexcept Exception:\n    pass",
        "",
        1,
    )
    ns: dict[str, object] = {}
    exec(compile(code, str(SESSION_START_HOOK), "exec"), ns)
    monkeypatch.setenv(
        "GOALFLIGHT_HOOK_INPUT",
        json.dumps(
            {
                "hook_event_name": "SessionStart",
                "source": "startup",
                "cwd": str(project),
            }
        ),
    )
    monkeypatch.setenv("GOALFLIGHT_PLUGIN_ROOT", str(ROOT))
    monkeypatch.setenv("GOALFLIGHT_WATCHDOG_RECENT_SECONDS", "0")
    ns["has_recent_resume_note"] = lambda: False
    ns["controller_wake_instruction"] = lambda *_args: "Wake ownership unknown."
    timeouts: list[float] = []

    def slow_unknown(*_args, timeout_s: float = 3.0) -> str:
        timeouts.append(timeout_s)
        time.sleep(timeout_s)
        return "unknown"

    def unexpected_claim(*_args, **_kwargs) -> dict[str, object]:
        raise AssertionError("unknown activity must not claim")

    ns["journal_activity"] = slow_unknown
    ns["session_status_active"] = slow_unknown
    ns["claim_controller_entry"] = unexpected_claim
    main = ns["main"]
    assert callable(main)

    started = time.monotonic()
    main()
    elapsed = time.monotonic() - started

    assert elapsed < 5.0
    assert len(timeouts) == 2
    assert sum(timeouts) <= 4.1
    payload = json.loads(capsys.readouterr().out)
    context = str(payload["hookSpecificOutput"]["additionalContext"])
    assert "could not determine Goal Flight activity" in context


def test_session_start_proven_inactive_is_silent_without_claiming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "inactive-project"
    project.mkdir()
    (project / "SKILL.md").write_text("hook inactive\n", encoding="utf-8")
    (project / "scripts").symlink_to(SCRIPTS, target_is_directory=True)
    status_script = tmp_path / "inactive-status.py"
    status_script.write_text(
        'print("no active goal-flight session (test)")\n',
        encoding="utf-8",
    )

    env = _session_start_test_env(
        tmp_path / "inactive-machine",
        monkeypatch,
        label="inactive-controller",
    )
    env["GOALFLIGHT_WATCHDOG_STATUS_SCRIPT"] = str(status_script)

    completed = _invoke_session_start_hook(project, env, source="startup")
    assert completed.stdout == ""
    assert completed.stderr == ""
    authority = journal.open_or_create_journal(project)
    assert authority.active_lease("inactive-controller") is None


def test_real_process_table_with_spaced_root_never_proves_absence(
    isolated: tuple[Path, journal.LeaseIdentity],
) -> None:
    try:
        real_ps = subprocess.run(
            ["ps", "-axww", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except OSError as exc:
        pytest.skip(f"real process-table probe unavailable: {exc}")
    if real_ps.returncode != 0:
        pytest.skip(
            "real process-table probe unavailable: "
            + (real_ps.stderr.strip() or f"exit {real_ps.returncode}")
        )

    fixture_project, fixture_lease = isolated
    project = fixture_project.parent / "real ps project"
    project.mkdir()
    authority = journal.open_or_create_journal(project)
    claimed = authority.claim_or_renew_lease(
        fixture_lease.label,
        principal={"principal_id": "real-ps-spaced-root"},
    )
    assert claimed.committed and claimed.value is not None
    lease = claimed.value
    holder = wake.register_lease_holder(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    )
    parts = shlex.split(
        wake.coverage_supervise_command(
            project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
        )
    )
    parts[0] = sys.executable
    supervisor = subprocess.Popen(
        parts,
        cwd=project,
        env=dict(os.environ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(0.2)
        assert supervisor.poll() is None
        state = wake.supervisor_generation_state(
            project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
        )
        assert state in {wake.SUPERVISOR_RUNNING, wake.SUPERVISOR_UNKNOWN}
        stream = __import__("io").StringIO()
        result = wake.check_tool_entry(
            project,
            controller_label=lease.label,
            controller_lease_nonce=lease.nonce,
            controller_claimed=True,
            mail_bearing=True,
            stream=stream,
        )
        assert result["rearm_plan"]["supervisor"] != wake.SUPERVISOR_ABSENT
        text = stream.getvalue()
        for component_command in _component_commands(project, lease):
            assert component_command not in text
    finally:
        holder.close()
        if supervisor.poll() is None:
            supervisor.terminate()
            try:
                supervisor.wait(timeout=5)
            except subprocess.TimeoutExpired:
                supervisor.kill()
                supervisor.wait(timeout=5)
        if supervisor.stdout is not None:
            supervisor.stdout.close()
        if supervisor.stderr is not None:
            supervisor.stderr.close()


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
    assert hint == ""
    activity = wake.listener_activity_hint(
        int(plan["live"]),
        int(plan["target"]),
        str(plan["command"]),
        work_in_flight=True,
        supervisor=str(plan["supervisor"]),
        supervise_command=str(plan["supervise_command"]),
    )
    assert activity == ""


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


def test_rearm_hint_undetermined_withholds_component_actions(
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
    assert "Otherwise" not in hint
    assert stream_cmd not in hint
    assert backup_cmd not in hint
    assert watchdog_cmd not in hint
    activity = wake.listener_activity_hint(
        int(plan["live"]),
        int(plan["target"]),
        str(plan["command"]),
        work_in_flight=True,
        supervisor=str(plan["supervisor"]),
        supervise_command=str(plan["supervise_command"]),
    )
    assert "If you are running `supervise`, restart it" in activity
    assert str(plan["command"]) not in activity
    assert "listener depth" not in activity
    assert f"{plan['live']}/{plan['target']}" not in activity


def test_unknown_operator_plan_has_reason_without_bare_component_action(
    isolated: tuple[Path, journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, lease = isolated
    authority = journal.Journal(project)
    assert authority.prepare_attempt("unknown-startup-action").committed
    monkeypatch.setattr(wake, "_process_listing", lambda: None)
    wake.activate_monitor_state(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        heartbeat_s=120,
        dead_after_s=360,
    )

    depth = sessions._listener_depth_after_claim(
        project,
        lease.label,
        lease.nonce,
    )

    assert depth is not None
    assert depth["supervisor"] == wake.SUPERVISOR_UNKNOWN
    assert "command" not in depth
    assert "commands" not in depth
    assert "live" not in depth
    assert "target" not in depth
    assert "missing" not in depth
    assert "missing_components" not in depth
    assert "supervise_command" not in depth
    encoded = json.dumps(depth)
    for component_command in _component_commands(project, lease):
        assert component_command not in encoded
    action = wake.supervisor_operator_action(
        str(depth["supervisor"]),
        component_command="MUST-NOT-SURVIVE",
    )
    assert action["kind"] == "verify-supervisor"
    assert action["command"] is None
    assert "could not tell whether `supervise`" in str(action["instruction"])
    assert "reported by status" not in str(action["instruction"])
    assert "confirming no supervisor is running" in str(action["instruction"])


def test_unknown_coverage_is_not_rendered_as_zero_shortfall(
    isolated: tuple[Path, journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, lease = isolated
    supervise_cmd = wake.coverage_supervise_command(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    )
    monkeypatch.setattr(
        wake,
        "_process_listing",
        lambda: [(4242, supervise_cmd)],
    )
    shutil.rmtree(wake.ledger_dir(project))
    status = wake.coverage_status(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    )
    assert status["reason"] == "waiter-probe-unavailable"
    assert status["live_waiters"] is None
    plan = wake.coverage_rearm_plan(
        status,
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        work_in_flight=True,
    )
    assert plan["supervisor"] == wake.SUPERVISOR_RUNNING
    assert plan["live"] is None
    assert plan["missing"] is None
    assert wake.coverage_rearm_hint(plan) == ""

    result = doctor.check_wake_coverage(project)
    assert result["ok"] is None
    pool = result["pools"][0]
    assert pool["supervisor"] == wake.SUPERVISOR_RUNNING
    assert pool["ok"] is None
    assert pool.get("reason") == "waiter-probe-unavailable"
    for field in (
        "covered",
        "live_waiters",
        "target_waiters",
        "missing_components",
    ):
        assert field not in pool
    lines = doctor.collect_human_lines(
        _minimal_doctor_payload(result)  # type: ignore[arg-type]
    )
    line = next(line for line in lines if "wake coverage hint-ctl" in line)
    assert "coverage=unknown" in line
    assert "supervisor=running" in line
    assert "coverage-probe=" not in line
    assert "0/" not in line
    assert "Restart the supervisor" not in line


def test_operator_action_policy_requires_proven_absence() -> None:
    component = "DIRECT COMPONENT"
    supervise_command = "python3 goalflight_messages.py supervise --lease-nonce n"

    running = wake.supervisor_operator_action(
        wake.SUPERVISOR_RUNNING,
        component_command=component,
        supervise_command=supervise_command,
    )
    absent = wake.supervisor_operator_action(
        wake.SUPERVISOR_ABSENT,
        component_command=component,
        supervise_command=supervise_command,
    )
    unknown = wake.supervisor_operator_action(
        wake.SUPERVISOR_UNKNOWN,
        component_command=component,
        supervise_command=supervise_command,
    )

    assert running["kind"] == "restart-supervisor"
    assert running["command"] == supervise_command
    assert component not in str(running["instruction"])
    assert absent["kind"] == "arm-component"
    assert absent["command"] == component
    assert unknown["kind"] == "verify-supervisor"
    assert unknown["command"] is None
    assert component not in str(unknown["instruction"])


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
    ) not in hint


@pytest.mark.parametrize("truncate_spaced_root", [False, True])
def test_missing_generation_nonce_uses_shared_detector_and_stays_unknown(
    isolated: tuple[Path, journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    truncate_spaced_root: bool,
) -> None:
    project, lease = isolated
    detected_root = project / "mission root" if truncate_spaced_root else project
    listed_root = (
        str(detected_root).split(" ", 1)[0]
        if truncate_spaced_root
        else str(detected_root)
    )
    command = (
        "python3 scripts/goalflight_messages.py supervise "
        f"--project-root {listed_root} --controller-label {lease.label} "
        f"--lease-nonce {lease.nonce}"
    )
    monkeypatch.setattr(wake, "_process_listing", lambda: [(4242, command)])

    assert wake.supervisor_generation_state(
        detected_root,
        controller_label=lease.label,
        lease_nonce="",
    ) == wake.SUPERVISOR_UNKNOWN


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
    assert hint == ""
    assert listen_cmd not in hint
    activity = wake.listener_activity_hint(
        int(plan["live"]),
        int(plan["target"]),
        str(plan["command"]),
        work_in_flight=True,
        supervisor=str(plan["supervisor"]),
        supervise_command=str(plan["supervise_command"]),
    )
    assert activity == ""


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


def _matching_supervise_tail(project: Path, lease: journal.LeaseIdentity) -> list[str]:
    return [
        "scripts/goalflight_messages.py",
        "supervise",
        "--project-root",
        str(project),
        "--controller-label",
        lease.label,
        "--lease-nonce",
        lease.nonce,
    ]


def _state_from_listing(
    project: Path,
    lease: journal.LeaseIdentity,
    listing: list[tuple[int | None, str]] | None,
) -> str:
    return wake._supervisor_generation_state_from_listing(
        listing,
        project_root=project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    )


def test_foreign_python_c_carrying_supervise_argv_is_not_running(
    isolated: tuple[Path, journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trailing supervise tokens on python3 -c are not a live supervisor."""
    project, lease = isolated
    foreign = (
        'python3 -c "import time; time.sleep(3600)" '
        + shlex.join(_matching_supervise_tail(project, lease))
    )
    listing = [(4242, foreign)]
    state = _state_from_listing(project, lease, listing)
    assert state != wake.SUPERVISOR_RUNNING
    assert state == wake.SUPERVISOR_ABSENT
    plan = _persistent_shortfall_plan(project, lease, monkeypatch, listing)
    assert plan["supervisor"] == wake.SUPERVISOR_ABSENT
    action = wake.supervisor_operator_action(
        str(plan["supervisor"]),
        component_command=str(plan.get("command") or ""),
        supervise_command=str(plan.get("supervise_command") or ""),
    )
    assert action["kind"] == "arm-component"


def test_genuine_supervise_argv_still_running(
    isolated: tuple[Path, journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Executable-position supervise still binds as running."""
    project, lease = isolated
    genuine = wake.coverage_supervise_command(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    )
    listing = [(4242, genuine)]
    assert _state_from_listing(project, lease, listing) == wake.SUPERVISOR_RUNNING
    with_u_parts = shlex.split(genuine)
    with_u_parts.insert(1, "-u")
    assert (
        _state_from_listing(project, lease, [(4243, shlex.join(with_u_parts))])
        == wake.SUPERVISOR_RUNNING
    )
    shebang = shlex.join(shlex.split(genuine)[1:])
    assert (
        _state_from_listing(project, lease, [(4244, shebang)])
        == wake.SUPERVISOR_RUNNING
    )
    assert (
        _state_from_listing(project, lease, [(4245, "env " + genuine)])
        == wake.SUPERVISOR_RUNNING
    )
    plan = _persistent_shortfall_plan(project, lease, monkeypatch, listing)
    assert plan["supervisor"] == wake.SUPERVISOR_RUNNING


def test_unparsable_or_truncated_supervise_listing_is_unknown(
    isolated: tuple[Path, journal.LeaseIdentity],
) -> None:
    """Unreadable executable position is UNKNOWN, never running or absent."""
    project, lease = isolated
    tail = shlex.join(_matching_supervise_tail(project, lease))
    unparsable = (
        "python3 scripts/goalflight_messages.py supervise "
        f"--project-root {project} --controller-label {lease.label} "
        f'--lease-nonce "{lease.nonce}'
    )
    truncated_python = "python3 --not-a-real-flag " + tail
    wrapper = "mystery-wrapper " + tail
    for command in (unparsable, truncated_python, wrapper):
        state = _state_from_listing(project, lease, [(8, command)])
        assert state == wake.SUPERVISOR_UNKNOWN, command
        action = wake.supervisor_operator_action(state, component_command="MUST-NOT")
        assert action["kind"] == "verify-supervisor"
        assert action["command"] is None
        assert "MUST-NOT" not in str(action["instruction"])


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
    assert line is None
    assert stream.getvalue() == ""
    once = wake.consume_listener_activity_signal(project, lease.label, plan)
    assert once == ""
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
    tmp_path: Path,
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
    env = _ps_listing_env(
        tmp_path,
        env,
        name="probe-absent-json-exit",
        rows=[],
    )
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


def test_direct_json_exit_beside_detected_supervisor_omits_rearm(
    isolated: tuple[Path, journal.LeaseIdentity],
    tmp_path: Path,
) -> None:
    project, lease = isolated
    authority = journal.Journal(project)
    assert authority.prepare_attempt("direct-json-listener-exit").committed
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
    supervise_cmd = wake.coverage_supervise_command(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    )
    env = dict(os.environ)
    env.pop("GOALFLIGHT_SUPERVISED", None)
    env = _ps_listing_env(
        tmp_path,
        env,
        name="probe-running-json-exit",
        rows=[f"4242 {supervise_cmd}"],
    )
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
    assert "rearm" not in payload
    assert "wake_recovery_hint" not in payload
    assert wake.persistent_backup_start_command(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    ) not in completed.stdout


def test_direct_json_exit_with_unavailable_process_table_is_numberless(
    isolated: tuple[Path, journal.LeaseIdentity],
    tmp_path: Path,
) -> None:
    project, lease = isolated
    authority = journal.Journal(project)
    assert authority.prepare_attempt("unknown-json-listener-exit").committed
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
    shim_dir = tmp_path / "probe-unknown-json-exit"
    shim_dir.mkdir()
    ps_shim = shim_dir / "ps"
    ps_shim.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    ps_shim.chmod(0o755)
    env = dict(os.environ)
    env.pop("GOALFLIGHT_SUPERVISED", None)
    env["PATH"] = f"{shim_dir}:{env.get('PATH', '')}"
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
    encoded = json.dumps(payload, sort_keys=True)
    assert completed.returncode == 1
    assert payload["reason"] == "timeout"
    assert "rearm" not in payload
    assert "could not tell whether `supervise`" in payload["wake_recovery_hint"]
    assert "0/" not in encoded
    assert "listener pool n=" not in encoded
    assert wake.persistent_backup_start_command(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    ) not in encoded


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
    if event_type == "watchdog-dead":
        assert "live" not in payload
        assert "target" not in payload
        assert "missing_components" not in payload
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
    isolated: tuple[Path, journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    builder,
    status: dict[str, object],
) -> None:
    project, lease = isolated
    monkeypatch.delenv("GOALFLIGHT_SUPERVISED", raising=False)
    monkeypatch.setattr(wake, "_process_listing", lambda: [])
    record = builder(
        status,
        project_root=project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        rearm_command="EXACT UNSUPERVISED COMMAND",
    )
    assert record["payload"]["rearm_command"] == "EXACT UNSUPERVISED COMMAND"
    if builder is messages._watchdog_dead_record:
        assert record["payload"]["live"] == 1
        assert record["payload"]["target"] == 8
        assert record["payload"]["missing_components"] == ["watchdog"]


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
def test_direct_dead_events_beside_detected_supervisor_omit_rearm_command(
    isolated: tuple[Path, journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    builder,
    status: dict[str, object],
) -> None:
    project, lease = isolated
    monkeypatch.delenv("GOALFLIGHT_SUPERVISED", raising=False)
    supervise_cmd = wake.coverage_supervise_command(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    )
    monkeypatch.setattr(
        wake,
        "_process_listing",
        lambda: [(4242, supervise_cmd)],
    )
    record = builder(
        status,
        project_root=project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        rearm_command="MUST NOT LEAK",
    )
    assert record["payload"]["reason"]
    assert "rearm_command" not in record["payload"]
    if builder is messages._watchdog_dead_record:
        assert "live" not in record["payload"]
        assert "target" not in record["payload"]
        assert "missing_components" not in record["payload"]
    assert "MUST NOT LEAK" not in json.dumps(record)


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
def test_dead_events_with_unavailable_process_table_stay_numberless_and_safe(
    isolated: tuple[Path, journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
    builder,
    status: dict[str, object],
) -> None:
    project, lease = isolated
    monkeypatch.delenv("GOALFLIGHT_SUPERVISED", raising=False)
    monkeypatch.setattr(wake, "_process_listing", lambda: None)

    record = builder(
        status,
        project_root=project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        rearm_command="MUST NOT LEAK",
    )
    payload = record["payload"]
    assert payload["reason"]
    assert "rearm_command" not in payload
    assert "live" not in payload
    assert "target" not in payload
    assert "missing_components" not in payload
    assert "MUST NOT LEAK" not in json.dumps(record)
    assert "could not tell whether `supervise`" in payload["wake_recovery_hint"]


def test_dead_event_contracts_scope_actions_to_unsupervised_paths() -> None:
    contract_paths = {
        ROOT / "protocols" / "controller-mail.md": (
            "each poll; stale, faulted, missing, or invalid state makes it emit"
        ),
        ROOT / "commands" / "execute.md": (
            "three missed heartbeat intervals emit `listener-dead`"
        ),
        ROOT / "docs" / "controller-behaviours.md": (
            "reads durable record age; three missed heartbeat intervals make it emit"
        ),
        ROOT / "docs" / "EVENT-ARCHITECTURE.md": (
            "and emits `event`/`listener-dead` when state"
        ),
        ROOT / "SKILL.md": (
            "In the decomposed unsupervised path, `listener-dead`"
        ),
    }
    for path, anchor in contract_paths.items():
        text = path.read_text(encoding="utf-8")
        start = text.index(anchor)
        contract = " ".join(text[start : start + 1000].split())
        assert "unsupervised path" in contract, path
        assert "supervise" in contract, path
        assert "reason" in contract, path
        assert "omit" in contract, path
        assert "supervisor restart" in contract, path

    fleet_contract = (
        ROOT / "protocols" / "fleet-console-producer.md"
    ).read_text(encoding="utf-8")
    assert "Proven supervisor absence carries the exact component" in fleet_contract
    assert "running supervisor suppresses controller-facing depth" in fleet_contract
    assert "Unknown supervisor state carries numberless" in fleet_contract
    assert "no component command" in fleet_contract


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
    parts = shlex.split(supervise_cmd)
    parts[0] = sys.executable
    supervise_env = dict(os.environ)
    supervise_env.pop("GOALFLIGHT_DISPATCH_ID", None)
    held_watchdog = wake.register_watchdog_waiter(
        project,
        controller_label=lease.label,
        generation_key=lease.nonce,
    )
    supervisor = subprocess.Popen(
        parts,
        cwd=project,
        env=supervise_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stop = _wait_for_supervisor_stop(supervisor, reason="did-not-arm")
        assert stop["scope"] == "slot"
        assert stop["child"] == "watchdog"
        assert supervisor.poll() is None

        # The competing watchdog is gone, but spawn_due retains the real
        # slot's stopped_reason.  The remaining three children stay armed.
        held_watchdog.close()
        held_watchdog = None
        deadline = time.monotonic() + 5.0
        while True:
            status = wake.coverage_status(
                project,
                controller_label=lease.label,
                lease_nonce=lease.nonce,
            )
            live = status.get("live_waiters")
            target = status.get("target_waiters")
            if (
                isinstance(live, int)
                and isinstance(target, int)
                and live == target - 1
                and "watchdog" in (status.get("missing_components") or [])
            ):
                break
            assert time.monotonic() < deadline, status
            time.sleep(0.05)
        assert supervisor.poll() is None
        time.sleep(0.2)
        settled = wake.coverage_status(
            project,
            controller_label=lease.label,
            lease_nonce=lease.nonce,
        )
        assert settled["live_waiters"] == live
        assert settled["target_waiters"] == target
        assert settled["missing_components"] == ["watchdog"]

        # Process discovery remains a separate concern; supply only the real
        # live process identity.  Coverage and stopped_reason came from the
        # production supervisor and wake ledger above.
        monkeypatch.setattr(
            wake,
            "_process_listing",
            lambda *args, **kwargs: [(supervisor.pid, supervise_cmd)],
        )
        result = doctor.check_wake_coverage(project)
        assert result["present"] is True
        assert result["pools"]
        pool = result["pools"][0]
        assert pool["label"] == lease.label
        assert pool["supervisor"] == wake.SUPERVISOR_RUNNING
        assert pool["ok"] is False
        assert result["ok"] is False
        assert pool["live_waiters"] == live
        assert pool["target_waiters"] == target
        assert pool["missing_components"] == ["watchdog"]
        assert pool["reason"]
        hint = str(pool["hint"])
        assert "Restart the supervisor" in hint
        assert supervise_cmd in hint
        encoded = json.dumps(pool)
        for component_command in _component_commands(project, lease):
            assert component_command not in encoded
            assert component_command not in hint
        lines = doctor.collect_human_lines(
            _minimal_doctor_payload(result)  # type: ignore[arg-type]
        )
        line = next(line for line in lines if "wake coverage hint-ctl" in line)
        assert "supervisor=running" in line
        assert "Restart the supervisor" in line
        assert supervise_cmd in line
        parsed = doctor.parse_status_line(line)
        assert parsed["level"] == "warn"

        _start_in_flight_attempt(project, lease, "stopped-slot-attention")
        attention = fleet._controller_attention_rows(
            [project],
            {"capacity_state": {"leases": {}}, "dispatch": {"records": []}},
        )
        rows = [row for row in attention if row["kind"] == "controller_hung"]
        assert len(rows) == 1
        assert rows[0]["action"] == supervise_cmd
        assert "wake coverage incomplete" in str(rows[0]["headline"])
        assert f"{live}/{target}" in str(rows[0]["headline"])
        assert "missing=watchdog" in str(rows[0]["headline"])
    finally:
        if held_watchdog is not None:
            held_watchdog.close()
        if supervisor.poll() is None:
            supervisor.terminate()
            try:
                supervisor.wait(timeout=5)
            except subprocess.TimeoutExpired:
                supervisor.kill()
                supervisor.wait(timeout=5)
        if supervisor.stdout is not None:
            supervisor.stdout.close()
        if supervisor.stderr is not None:
            supervisor.stderr.close()


def test_stopped_slot_is_not_healthy(
    isolated: tuple[Path, journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live supervisor that will not respawn a slot is not doctor-green."""
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
    pool = result["pools"][0]
    assert pool["ok"] is False
    assert result["ok"] is False
    assert pool["supervisor"] == wake.SUPERVISOR_RUNNING
    action = wake.supervisor_operator_action(
        wake.SUPERVISOR_RUNNING,
        supervise_command=supervise_cmd,
    )
    assert action["kind"] == "restart-supervisor"
    assert str(action["instruction"]) == str(pool["hint"])
    assert supervise_cmd in str(pool["hint"])
    encoded = json.dumps(pool)
    for component_command in _component_commands(project, lease):
        assert component_command not in encoded


def test_doctor_supervised_full_pool_stays_green(
    isolated: tuple[Path, journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, lease = isolated
    supervise_cmd = wake.coverage_supervise_command(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    )
    monkeypatch.setattr(
        wake,
        "_process_listing",
        lambda *args, **kwargs: [(4242, supervise_cmd)],
    )
    wake.activate_monitor_state(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        heartbeat_s=120,
        dead_after_s=360,
    )
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            wake.register_waiter(
                project,
                controller_label=lease.label,
                kind=wake.MONITOR_KIND,
                generation_key=lease.nonce,
            )
        )
        backup_slots = wake.persistent_backup_slot_count()
        for _index in range(backup_slots):
            stack.enter_context(
                wake.register_listener_waiter(
                    project,
                    controller_label=lease.label,
                    generation_key=lease.nonce,
                    slots=backup_slots,
                )
            )
        stack.enter_context(
            wake.register_watchdog_waiter(
                project,
                controller_label=lease.label,
                generation_key=lease.nonce,
            )
        )
        result = doctor.check_wake_coverage(project)
        pool = result["pools"][0]
        assert pool["supervisor"] == wake.SUPERVISOR_RUNNING
        assert pool["ok"] is True
        assert result["ok"] is True
        assert "hint" not in pool
        assert "reason" not in pool
        for field in (
            "covered",
            "live_waiters",
            "target_waiters",
            "missing_components",
        ):
            assert field not in pool
        lines = doctor.collect_human_lines(
            _minimal_doctor_payload(result)  # type: ignore[arg-type]
        )
        line = next(line for line in lines if "wake coverage hint-ctl" in line)
        assert "supervisor=running" in line
        assert "Restart the supervisor" not in line
        parsed = doctor.parse_status_line(line)
        assert parsed["detail"] == "supervisor=running"
        assert parsed["level"] == "ok"
        _start_in_flight_attempt(project, lease, "healthy-pool-attention")
        attention = fleet._controller_attention_rows(
            [project],
            {"capacity_state": {"leases": {}}, "dispatch": {"records": []}},
        )
        assert not [
            row for row in attention if row["kind"] == "controller_hung"
        ]


def test_doctor_unknown_machine_payload_is_numberless(
    isolated: tuple[Path, journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, lease = isolated
    _persistent_shortfall_plan(project, lease, monkeypatch, None)

    result = doctor.check_wake_coverage(project)
    assert result["ok"] is None
    pool = result["pools"][0]
    assert pool["label"] == lease.label
    assert pool["supervisor"] == wake.SUPERVISOR_UNKNOWN
    assert pool["ok"] is None
    assert "could not tell whether `supervise`" in str(pool["hint"])
    for field in (
        "covered",
        "live_waiters",
        "target_waiters",
        "missing_components",
    ):
        assert field not in pool
    encoded = json.dumps(pool)
    assert "1/8" not in encoded
    for component_command in _component_commands(project, lease):
        assert component_command not in encoded


def test_doctor_unknown_supervisor_json_verdict_is_not_ok(
    isolated: tuple[Path, journal.LeaseIdentity],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreadable process listing must not certify the session green."""
    project, lease = isolated
    _persistent_shortfall_plan(project, lease, monkeypatch, None)
    result = doctor.check_wake_coverage(project)
    assert result["present"] is True
    pool = result["pools"][0]
    assert pool["supervisor"] == wake.SUPERVISOR_UNKNOWN
    assert pool["ok"] is None
    assert result["ok"] is None
    for field in (
        "covered",
        "live_waiters",
        "target_waiters",
        "missing_components",
    ):
        assert field not in pool
    payload = _minimal_doctor_payload(result)  # type: ignore[arg-type]
    lines = doctor.collect_human_lines(payload)
    line = next(line for line in lines if f"wake coverage {lease.label}" in line)
    parsed = doctor.parse_status_line(line)
    assert parsed["level"] == "warn"
    assert "coverage=unknown" in parsed["detail"]
    assert "supervisor=unknown" in parsed["detail"]
    assert "0/" not in parsed["detail"]
    summary = doctor.verdict_summary(payload)
    assert summary["verdict"] != "ok"
    assert summary["verdict"] == "warn"
