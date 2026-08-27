"""Floor + attention doorbell contracts for t-267.

Doctrine (a): the controller issues N tracked arms. Tooling makes the
remaining-depth command obvious after every listen exit and at lease
claim, and refuses a single detached call.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_session_status as sessions  # noqa: E402
import goalflight_wake as wake  # noqa: E402


@pytest.fixture()
def isolated(monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, str]]:
    td = Path(tempfile.mkdtemp(prefix="gf-listener-floor-"))
    env = {
        "GOALFLIGHT_JOURNAL_DIR": str(td / "journals"),
        "GOALFLIGHT_STATE_DIR": str(td / "state"),
        "GOALFLIGHT_WAKE_LEDGER_DIR": str(td / "wake-ledger"),
        "GOALFLIGHT_MESSAGES_DIR": str(td / "messages"),
        "GOALFLIGHT_TASK_STORE_DIR": str(td / "task-store"),
        "GOAL_FLIGHT_PIDFILE_DIR": str(td / "pids"),
        "GOALFLIGHT_CAPACITY_CONF": os.devnull,
        "GOALFLIGHT_TEST_MODE": "1",
        "GOALFLIGHT_TEST_LISTENER_START_TOKEN": "floor-listener-token",
    }
    for value in env.values():
        if value != os.devnull:
            Path(value).mkdir(parents=True, exist_ok=True)
    ps_dir = td / "empty-process-listing"
    ps_dir.mkdir()
    ps_shim = ps_dir / "ps"
    ps_shim.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-axww\" ]; then exit 0; fi\n"
        "exec /bin/ps \"$@\"\n",
        encoding="utf-8",
    )
    ps_shim.chmod(0o755)
    env["PATH"] = f"{ps_dir}:{os.environ.get('PATH', '')}"
    monkeypatch.setattr(wake, "_process_listing", lambda: [])
    for key, value in env.items():
        if key != "PATH":
            monkeypatch.setenv(key, value)
    monkeypatch.delenv("GOALFLIGHT_CONTROLLER_LABEL", raising=False)
    monkeypatch.delenv("GOALFLIGHT_CONTROLLER_LEASE_NONCE", raising=False)
    monkeypatch.delenv("GOALFLIGHT_DISPATCH_ID", raising=False)
    project = td / "project"
    project.mkdir()
    return project, {**os.environ, **env}


def _claim(project: Path, label: str = "floor-ctl") -> journal.LeaseIdentity:
    authority = journal.open_or_create_journal(project)
    claimed = authority.claim_or_renew_lease(
        label, principal={"principal_id": f"{label}-principal"}
    )
    assert claimed.committed and claimed.value is not None
    return claimed.value


def _listen_cmd(project: Path, *, label: str, nonce: str, timeout_s: float = 20) -> list[str]:
    return [
        sys.executable,
        str(SCRIPTS / "goalflight_messages.py"),
        "listen",
        "--project-root",
        str(project),
        "--controller-label",
        label,
        "--lease-nonce",
        nonce,
        "--poll-secs",
        "0.01",
        "--timeout-s",
        str(timeout_s),
        "--json",
        "--report-pending",
    ]


def _wait_live(project: Path, label: str, count: int, *, timeout_s: float = 5) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        waiters = wake.live_waiters(project, controller_label=label) or []
        if len(waiters) == count:
            return
        time.sleep(0.02)
    raise AssertionError(f"live waiters for {label} never reached {count}")


def _post(env: dict[str, str], project: Path, label: str, text: str) -> None:
    subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "goalflight_messages.py"),
            "post",
            "--to-controller",
            label,
            "--dispatch-id",
            "floor-mail",
            "--type",
            "controller-notice",
            "--text",
            text,
        ],
        env=env,
        cwd=project,
        check=True,
        capture_output=True,
    )


def test_floor_hint_names_missing_slots_as_separate_tracked_calls() -> None:
    command = "python3 scripts/goalflight_messages.py listen --report-pending"
    unknown = wake.listener_floor_hint(0, 4, command, work_in_flight=True)
    assert "listener coverage needs verification" in unknown
    assert command not in unknown
    hint = wake.listener_floor_hint(
        0,
        4,
        command,
        work_in_flight=True,
        supervisor=wake.SUPERVISOR_ABSENT,
    )
    assert "live=0/4" in hint
    assert "4 slots missing" in hint
    assert wake.SEPARATE_TRACKED_ARM_RULE in hint
    assert hint.count(command) == 4
    assert "1. " in hint and "4. " in hint
    assert "&" in hint
    assert wake.listener_floor_hint(0, 4, command, work_in_flight=False) == ""
    assert wake.listener_floor_hint(4, 4, command, work_in_flight=True) == ""
    thin = wake.listener_floor_hint(
        1,
        4,
        command,
        work_in_flight=True,
        supervisor=wake.SUPERVISOR_ABSENT,
    )
    assert thin.startswith("listener pool n=1/4 — 3 slots missing")
    assert thin.count(command) == 3


def test_depth_plan_is_idempotent_at_target() -> None:
    plan = wake.listener_depth_plan(
        4, 4, "CMD", work_in_flight=True
    )
    assert plan["missing"] == 0
    assert plan["command"] == "CMD"
    assert "commands" not in plan
    assert "hint" not in plan
    short = wake.listener_depth_plan(0, 4, "CMD", work_in_flight=True)
    assert short["missing"] == 4
    assert short["command"] == "CMD"


def test_controller_attention_is_quiet_and_addressed_to_source(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, _env = isolated
    authority = journal.open_or_create_journal(project)
    lease = _claim(project)
    prepared = authority.prepare_attempt("floor-hung-work")
    assert prepared.committed
    armed = authority.arm_listener(
        "floor-ctl",
        nonce=lease.nonce,
        pid=os.getpid(),
        start_token="floor-token",
        parent_pid=os.getppid() or os.getpid(),
    )
    assert armed.committed and armed.value is not None
    exited = authority.exit_listener(str(armed.value["coverage_id"]), reason="orphaned")
    assert exited.committed

    items = authority.attention_items()
    assert items, "orphaned work must remain on the attention plane"
    assert len(items) == 1
    assert items[0]["item_type"] == "orphaned_controller_work"
    assert items[0]["state"] == "OPEN"
    assert items[0]["source_label"] == "floor-ctl"

    rows = authority.read_all(
        """
        SELECT recipient_label, wake_class, event_type
        FROM delivery_events WHERE event_type = 'controller_attention'
        """
    )
    assert len(rows) == 1
    assert rows[0]["recipient_label"] == "floor-ctl"
    assert rows[0]["wake_class"] == "quiet"
    assert rows[0]["event_type"] == "controller_attention"


def test_peer_waking_peek_does_not_consume_a_slot_on_attention(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, _env = isolated
    authority = journal.open_or_create_journal(project)
    source = _claim(project, "source-ctl")
    peer = _claim(project, "peer-ctl")
    assert authority.prepare_attempt("peer-attention-work").committed
    armed = authority.arm_listener(
        "source-ctl",
        nonce=source.nonce,
        pid=os.getpid(),
        start_token="source-token",
        parent_pid=os.getppid() or os.getpid(),
    )
    assert armed.committed and armed.value is not None
    assert authority.exit_listener(
        str(armed.value["coverage_id"]), reason="orphaned"
    ).committed

    peer_waking = authority.cursor_peek("peer-ctl", nonce=peer.nonce, waking_only=True)
    assert all(
        str(item.get("event_type") or "") != "controller_attention"
        for item in peer_waking.items
    )
    source_waking = authority.cursor_peek(
        "source-ctl", nonce=source.nonce, waking_only=True
    )
    assert all(
        str(item.get("event_type") or "") != "controller_attention"
        for item in source_waking.items
    )
    source_all = authority.cursor_peek("source-ctl", nonce=source.nonce, waking_only=False)
    attention = [
        item
        for item in source_all.items
        if str(item.get("event_type") or "") == "controller_attention"
    ]
    assert len(attention) == 1
    assert str(attention[0]["wake_class"]) == "quiet"
    envelope = messages._listener_envelope(authority, dict(attention[0]))
    assert envelope["type"] == "controller_attention"
    assert envelope["payload"]["type"] == "orphaned_controller_work"


def test_attention_plane_still_carries_hung_controller(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    """Operator surfaces keep HUNG / orphaned-work even when the doorbell is quiet."""
    import goalflight_fleet_console as console

    project, _env = isolated
    authority = journal.open_or_create_journal(project)
    lease = _claim(project)
    assert authority.prepare_attempt("hung-visible").committed
    armed = authority.arm_listener(
        "floor-ctl",
        nonce=lease.nonce,
        pid=os.getpid(),
        start_token="hung-token",
        parent_pid=os.getppid() or os.getpid(),
    )
    assert armed.committed and armed.value is not None
    assert authority.exit_listener(
        str(armed.value["coverage_id"]), reason="orphaned"
    ).committed
    items = authority.attention_items()
    assert items, "orphaned work must remain on the attention plane"
    assert items[0]["state"] == "OPEN"

    machine_status = {
        "capacity_state": {"leases": {}},
        "dispatch": {
            "records": [
                {
                    "controller_session_id": lease.nonce,
                    "controller_label": "floor-ctl",
                    "project_root": str(project.resolve()),
                    "state": "running",
                    "dispatch_id": "hung-visible",
                }
            ]
        },
        "rate_pressure": {},
    }
    with wake.register_lease_holder(
        project, controller_label="floor-ctl", lease_nonce=lease.nonce
    ):
        rows = console._controller_attention_rows([project], machine_status)
    assert any("HUNG" in str(row.get("headline") or "") for row in rows)
    assert any("floor-ctl" in str(row.get("headline") or "") for row in rows)


def test_consumed_slot_with_work_in_flight_emits_floor_and_following_it_restores_depth(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    """North star: in-flight work whose last slot is consumed is not left at zero."""
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    lease = _claim(project)
    assert authority.prepare_attempt("north-star-work").committed
    with wake.register_lease_holder(
        project, controller_label="floor-ctl", lease_nonce=lease.nonce
    ):
        cmd = _listen_cmd(project, label="floor-ctl", nonce=lease.nonce)
        proc = subprocess.Popen(
            cmd,
            cwd=project,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _wait_live(project, "floor-ctl", 1)
            _post(env, project, "floor-ctl", "worker result")
            stdout, stderr = proc.communicate(timeout=30)
            assert proc.returncode == 0, stderr
            payload = json.loads(stdout)
            assert payload["kind"] == "ring"
            rearm = payload["rearm"]
            assert rearm["work_in_flight"] is True
            assert rearm["live"] == 0
            assert rearm["missing"] == wake.DEFAULT_LISTENER_SLOTS
            assert rearm["command"]
            assert "commands" not in rearm
            assert wake.SEPARATE_TRACKED_ARM_RULE in rearm["hint"]
            assert "1. " in rearm["hint"]
            assert proc.poll() is not None
            waiters = wake.live_waiters(project, controller_label="floor-ctl") or []
            assert waiters == []

            # Following ONE printed command as its own tracked child restores
            # the floor. A `&` loop is the rejected shape.
            replacement = subprocess.Popen(
                cmd,
                cwd=project,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                _wait_live(project, "floor-ctl", 1)
                live = wake.live_waiters(project, controller_label="floor-ctl") or []
                assert len(live) == 1
                assert live[0].pid == replacement.pid
                listed = subprocess.check_output(
                    ["ps", "-o", "ppid=", "-p", str(replacement.pid)],
                    text=True,
                ).strip()
                assert int(listed) == os.getpid()
                assert int(listed) != 1
            finally:
                if replacement.poll() is None:
                    replacement.kill()
                    replacement.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def test_detached_single_call_is_refused_not_a_floor(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    lease = _claim(project)
    journal.open_or_create_journal(project).prepare_attempt("detached-work")
    env = dict(env)
    env["GOALFLIGHT_LISTENER_STARTUP_GRACE_S"] = "0.2"
    cmd = _listen_cmd(project, label="floor-ctl", nonce=lease.nonce, timeout_s=10)
    stdout_path = project.parent / "detached.stdout"
    stderr_path = project.parent / "detached.stderr"
    pid_path = project.parent / "detached.pid"
    launcher = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import subprocess; from pathlib import Path; "
                f"out=open({str(stdout_path)!r},'w'); err=open({str(stderr_path)!r},'w'); "
                f"p=subprocess.Popen({cmd!r},cwd={str(project)!r},stdout=out,stderr=err,"
                "text=True,start_new_session=True); "
                f"Path({str(pid_path)!r}).write_text(str(p.pid)); out.close(); err.close()"
            ),
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert launcher.returncode == 0, launcher.stderr
    listener_pid = int(pid_path.read_text(encoding="utf-8"))
    try:
        deadline = time.monotonic() + 5
        lines: list[str] = []
        while time.monotonic() < deadline:
            if stderr_path.exists():
                lines = stderr_path.read_text(encoding="utf-8").splitlines()
                if lines:
                    break
            time.sleep(0.05)
        assert len(lines) == 1
        assert lines[0].startswith("DETACHED LISTENER:")
        assert "tracked background task" in lines[0]
        _wait_live(project, "floor-ctl", 0)
    finally:
        try:
            os.kill(listener_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_arming_past_target_is_not_refused(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    lease = _claim(project)
    journal.open_or_create_journal(project).prepare_attempt("full-pool")
    command = wake.listener_start_command(project, controller_label="floor-ctl")
    holders = [
        wake.register_listener_waiter(
            project,
            controller_label="floor-ctl",
            generation_key=lease.nonce,
        )
        for _ in range(wake.DEFAULT_LISTENER_SLOTS)
    ]
    try:
        plan = wake.listener_depth_plan(
            wake.DEFAULT_LISTENER_SLOTS,
            wake.DEFAULT_LISTENER_SLOTS,
            command,
            work_in_flight=True,
        )
        assert plan["missing"] == 0
        assert plan["command"] == command
        assert "commands" not in plan
        assert "hint" not in plan
        extra = wake.register_listener_waiter(
            project,
            controller_label="floor-ctl",
            generation_key=lease.nonce,
        )
        try:
            live = wake.live_waiters(project, controller_label="floor-ctl") or []
            assert len(live) == wake.DEFAULT_LISTENER_SLOTS + 1
            assert extra.slot_index == wake.DEFAULT_LISTENER_SLOTS
        finally:
            extra.close()
        live = wake.live_waiters(project, controller_label="floor-ctl") or []
        assert len(live) == wake.DEFAULT_LISTENER_SLOTS
    finally:
        for holder in holders:
            holder.close()


def test_lease_claim_emits_remaining_depth_when_work_is_in_flight(
    isolated: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _env = isolated
    authority = journal.open_or_create_journal(project)
    assert authority.prepare_attempt("claim-floor-work").committed
    monkeypatch.setattr(
        sessions,
        "_controller_process_identity",
        lambda pid: {"pid": pid, "start_token": "claim-floor-token"},
    )
    monkeypatch.setattr(wake, "_process_listing", lambda: [])
    result = sessions.claim_controller_startup(
        project, pid=71001, label="floor-ctl", role="controller"
    )
    assert result["claimed"] is True
    depth = result["listener_depth"]
    assert depth["work_in_flight"] is True
    assert depth["live"] is None
    assert depth["missing"] is None
    assert isinstance(depth["command"], str) and depth["command"]
    assert "commands" not in depth
    assert "hint" not in depth


def test_lease_claim_stays_silent_without_in_flight_work(
    isolated: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _env = isolated
    monkeypatch.setattr(
        sessions,
        "_controller_process_identity",
        lambda pid: {"pid": pid, "start_token": "quiet-claim-token"},
    )
    monkeypatch.setattr(wake, "_process_listing", lambda: [])
    result = sessions.claim_controller_startup(
        project, pid=71002, label="floor-ctl", role="controller"
    )
    assert result["claimed"] is True
    depth = result["listener_depth"]
    assert depth["work_in_flight"] is False
    assert isinstance(depth["command"], str) and depth["command"]
    assert "commands" not in depth
    assert "hint" not in depth
