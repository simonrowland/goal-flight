"""Held-lock wake coverage, poll fallback, and universal entry contracts."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import shlex
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_dispatch as dispatch  # noqa: E402
import goalflight_ledger as ledger  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_session_status as sessions  # noqa: E402
import goalflight_status as status  # noqa: E402
import goalflight_wake as wake  # noqa: E402


def isolated_env(tmp_path: Path, *, label: str = "wake-test") -> dict[str, str]:
    env = dict(os.environ)
    for key in (
        "GOALFLIGHT_DISPATCH_ID",
        "GOALFLIGHT_CONTROLLER_SESSION_ID",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE",
        "GOALFLIGHT_CONTROLLER_PID",
    ):
        env.pop(key, None)
    env.update(
        {
            "GOALFLIGHT_MESSAGES_DIR": str(tmp_path / "messages"),
            "GOALFLIGHT_FLEET_DIR": str(tmp_path / "fleet"),
            "GOALFLIGHT_JOURNAL_DIR": str(tmp_path / "journals"),
            "GOALFLIGHT_TASK_STORE_DIR": str(tmp_path / "task-store"),
            "GOALFLIGHT_STATE_DIR": str(tmp_path / "state"),
            "GOALFLIGHT_WAKE_LEDGER_DIR": str(tmp_path / "wake-ledger"),
            "GOAL_FLIGHT_PIDFILE_DIR": str(tmp_path / "pids"),
            "GOALFLIGHT_CAPACITY_CONF": "/dev/null",
            "GOALFLIGHT_CONTROLLER_LABEL": label,
            "GOALFLIGHT_PROCESS_ROLE": "controller",
            "GOALFLIGHT_TEST_MODE": "1",
            "GOALFLIGHT_WAKE_ENTRY_POLL_S": "0",
            "GOALFLIGHT_TEST_LISTENER_START_TOKEN": "test-listener-token",
        }
    )
    return env


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, dict[str, str]]:
    env = isolated_env(tmp_path)
    for key in (
        "GOALFLIGHT_DISPATCH_ID",
        "GOALFLIGHT_CONTROLLER_SESSION_ID",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE",
        "GOALFLIGHT_CONTROLLER_PID",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        if key.startswith("GOAL"):
            monkeypatch.setenv(key, value)
    root = tmp_path / "project"
    root.mkdir()
    return root, env


def _spawn_lock_holder(root: Path, env: dict[str, str], *, kind: str = "listener") -> subprocess.Popen[str]:
    code = (
        "import sys,time; "
        f"sys.path.insert(0,{str(SCRIPTS)!r}); "
        "import goalflight_wake as w; "
        f"r=w.register_waiter({str(root)!r},controller_label='wake-test',kind={kind!r}); "
        "print(r.record.path,flush=True); time.sleep(60)"
    )
    return subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _spawn_lease_lock_holder(
    root: Path,
    env: dict[str, str],
    *,
    label: str,
    nonce: str,
) -> subprocess.Popen[str]:
    code = (
        "import sys,time; "
        f"sys.path.insert(0,{str(SCRIPTS)!r}); "
        "import goalflight_wake as w; "
        f"r=w.register_lease_holder({str(root)!r},controller_label={label!r},"
        f"lease_nonce={nonce!r}); "
        "print(r.record.path,flush=True); time.sleep(60)"
    )
    return subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _completion_dispatch_command(
    root: Path,
    tmp_path: Path,
    dispatch_id: str,
) -> list[str]:
    return [
        sys.executable,
        str(SCRIPTS / "goalflight_dispatch.py"),
        "--agent",
        "wake-test-worker",
        "--dispatch-id",
        dispatch_id,
        "--tail",
        str(tmp_path / f"{dispatch_id}.tail"),
        "--status-json",
        str(tmp_path / f"{dispatch_id}.status.json"),
        "--cwd",
        str(root),
        "--poll-secs",
        "0.02",
        "--max-idle-secs",
        "20",
        "--foreground",
        "--",
        sys.executable,
        "-c",
        f"print('COMPLETE: {dispatch_id} — done', flush=True)",
    ]


def _listener_command(
    root: Path,
    tmp_path: Path,
    *,
    label: str,
    nonce: str,
) -> list[str]:
    return [
        sys.executable,
        str(SCRIPTS / "goalflight_messages.py"),
        "--messages-dir",
        str(tmp_path / "messages"),
        "listen",
        "--project-root",
        str(root),
        "--controller-label",
        label,
        "--lease-nonce",
        nonce,
        "--poll-secs",
        "0.01",
        "--timeout-s",
        "8",
        "--json",
    ]


def _wait_for_listener(
    authority: journal.Journal,
    label: str,
    listener: subprocess.Popen[str],
) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        coverage = authority.active_coverage(label)
        if coverage is not None and coverage["pid"] == listener.pid:
            return
        time.sleep(0.01)
    raise AssertionError(f"listener for {label} never armed")


def _advance_all(
    authority: journal.Journal,
    *,
    label: str,
    nonce: str,
) -> None:
    peek = authority.cursor_peek(label, nonce=nonce, waking_only=False)
    advances: dict[str, int] = {}
    for item in peek.items:
        stream_id = str(item["stream_id"])
        advances[stream_id] = max(advances.get(stream_id, 0), int(item["stream_seq"]))
    assert advances
    advanced = authority.advance_cursor(
        label,
        nonce=nonce,
        expected_cursor_version=peek.cursor_version,
        expected_stream_snapshots=peek.stream_snapshots,
        advances=advances,
        actor="wake-finish-test",
    )
    assert advanced.committed, advanced.reason


def test_dispatch_inherits_ambient_lease_nonce_without_claim(
    isolated: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _env = isolated
    authority = journal.open_or_create_journal(root)
    process_identity = sessions._controller_process_identity(os.getpid())
    assert process_identity is not None
    claimed = authority.claim_or_renew_lease(
        "wake-test",
        principal={**process_identity, "hostname": "test-host"},
    )
    assert claimed.committed and claimed.value is not None
    lease = claimed.value
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_LEASE_NONCE", lease.nonce)
    monkeypatch.delenv("GOALFLIGHT_CONTROLLER_SESSION_ID", raising=False)
    monkeypatch.setattr(
        sessions,
        "claim_controller_startup",
        lambda *args, **kwargs: pytest.fail("capability inheritance attempted to claim"),
    )
    args = SimpleNamespace(
        agent="wake-test-worker",
        shape="bash",
        dispatch_id="ambient-owner",
        controller_label=None,
        controller_beacon_pid=None,
        controller_session_id=None,
        from_queue=False,
        launch_detached=False,
        acp_detached_child=False,
        takeover=False,
        task_ids=[],
        parent_dispatch_id=None,
        codex_session_id=None,
        codex_resume_home=None,
        codex_home_owner_dispatch_id=None,
    )
    with wake.register_lease_holder(
        root,
        controller_label="wake-test",
        lease_nonce=lease.nonce,
    ):
        result = dispatch._stamp_controller_session(args, root)
    assert result == {
        "claimed": False,
        "reason": "inherited_controller_capability",
        "inherited": True,
    }
    metadata = dispatch._prelaunch_status_metadata(args)
    assert metadata["controller_label"] == "wake-test"
    assert metadata["controller_session_id"] == lease.nonce
    assert metadata["controller_pid"] == os.getpid()
    assert authority.active_lease("wake-test") == lease


def test_owned_worker_finish_wakes_armed_doorbell_three_runs(
    isolated: tuple[Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    root, env = isolated
    authority = journal.open_or_create_journal(root)
    process_identity = sessions._controller_process_identity(os.getpid())
    assert process_identity is not None
    claimed = authority.claim_or_renew_lease(
        "wake-test",
        principal={**process_identity, "hostname": "test-host"},
    )
    assert claimed.committed and claimed.value is not None
    lease = claimed.value
    env["GOALFLIGHT_CONTROLLER_LEASE_NONCE"] = lease.nonce
    env.pop("GOALFLIGHT_CONTROLLER_SESSION_ID", None)
    env["GOALFLIGHT_CAPACITY_WAIT_S"] = "0"
    measurements: list[float] = []
    with wake.register_lease_holder(
        root,
        controller_label="wake-test",
        lease_nonce=lease.nonce,
    ):
        for run in range(1, 4):
            dispatch_id = f"owned-finish-{run}"
            listener = subprocess.Popen(
                _listener_command(
                    root,
                    tmp_path,
                    label="wake-test",
                    nonce=lease.nonce,
                ),
                cwd=root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                _wait_for_listener(authority, "wake-test", listener)
                started = time.monotonic()
                completed = subprocess.run(
                    _completion_dispatch_command(root, tmp_path, dispatch_id),
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=20,
                )
                listener_stdout, listener_stderr = listener.communicate(timeout=6)
                elapsed = time.monotonic() - started
                assert completed.returncode == 0, (completed.stdout, completed.stderr)
                assert listener.returncode == 0, listener_stderr
                assert json.loads(listener_stdout)["reason"] == "event"
                assert elapsed < 5.0
                measurements.append(elapsed)
                record = json.loads(ledger.record_path(dispatch_id, create=False).read_text())
                assert record["controller_label"] == "wake-test", (
                    record,
                    completed.stdout,
                    completed.stderr,
                )
                assert record["controller_session_id"] == lease.nonce
                attempt_owner = authority.read_all(
                    """SELECT owner_controller_label, owner_session_digest
                       FROM dispatch_attempts WHERE dispatch_id = ?""",
                    (dispatch_id,),
                )[0]
                assert attempt_owner["owner_controller_label"] == "wake-test"
                assert attempt_owner["owner_session_digest"] == (
                    wake.controller_session_digest(lease.nonce)
                )
                assert lease.nonce not in tuple(attempt_owner)
                rows = authority.read_all(
                    """SELECT recipient_label FROM delivery_events
                       WHERE stream_id = ? ORDER BY recipient_label""",
                    (dispatch_id,),
                )
                assert [str(row["recipient_label"]) for row in rows] == ["wake-test"]
                print(f"OWNED_FINISH_DOORBELL run={run} seconds={elapsed:.3f}")
                _advance_all(authority, label="wake-test", nonce=lease.nonce)
            finally:
                if listener.poll() is None:
                    listener.kill()
                    listener.communicate(timeout=3)
    assert len(measurements) == 3


def test_unowned_worker_finish_fans_out_and_wakes_registered_controller(
    isolated: tuple[Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    root, env = isolated
    authority = journal.open_or_create_journal(root)
    first = authority.claim_or_renew_lease(
        "wake-test",
        principal={"principal_id": "unowned-fanout-first"},
    )
    second = authority.claim_or_renew_lease(
        "second-controller",
        principal={"principal_id": "unowned-fanout-second"},
    )
    assert first.committed and first.value is not None
    assert second.committed and second.value is not None
    dispatch_env = dict(env)
    for key in (
        "GOALFLIGHT_CONTROLLER_LABEL",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE",
        "GOALFLIGHT_CONTROLLER_SESSION_ID",
        "GOALFLIGHT_CONTROLLER_PID",
    ):
        dispatch_env.pop(key, None)
    dispatch_env["GOALFLIGHT_CAPACITY_WAIT_S"] = "0"
    dispatch_id = "unowned-finish-fanout"
    listener = subprocess.Popen(
        _listener_command(
            root,
            tmp_path,
            label="wake-test",
            nonce=first.value.nonce,
        ),
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_listener(authority, "wake-test", listener)
        started = time.monotonic()
        completed = subprocess.run(
            _completion_dispatch_command(root, tmp_path, dispatch_id),
            cwd=root,
            env=dispatch_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        listener_stdout, listener_stderr = listener.communicate(timeout=6)
        elapsed = time.monotonic() - started
        assert completed.returncode == 0, (completed.stdout, completed.stderr)
        assert listener.returncode == 0, listener_stderr
        assert json.loads(listener_stdout)["reason"] == "event"
        assert elapsed < 5.0
        record = json.loads(ledger.record_path(dispatch_id, create=False).read_text())
        assert not record.get("controller_label")
        rows = authority.read_all(
            """SELECT recipient_label, projected_at FROM delivery_events
               WHERE stream_id = ? ORDER BY recipient_label""",
            (dispatch_id,),
        )
        assert [str(row["recipient_label"]) for row in rows] == [
            "second-controller",
            "wake-test",
        ]
        assert all(row["projected_at"] is not None for row in rows)
        print(
            "UNOWNED_FINISH_FANOUT "
            f"seconds={elapsed:.3f} recipients=second-controller,wake-test"
        )
    finally:
        if listener.poll() is None:
            listener.kill()
            listener.communicate(timeout=3)


def test_unowned_terminal_replacement_withdraws_every_fanout_recipient(
    isolated: tuple[Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    root, _env = isolated
    authority = journal.open_or_create_journal(root)
    for label in ("first-controller", "second-controller"):
        claimed = authority.claim_or_renew_lease(
            label,
            principal={"principal_id": f"replacement-{label}"},
        )
        assert claimed.committed and claimed.value is not None

    dispatch_id = "unowned-terminal-replacement"
    record_path = ledger.record_path(dispatch_id)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        json.dumps(
            {
                "dispatch_id": dispatch_id,
                "project_root": str(root),
                "state": "running",
            }
        ),
        encoding="utf-8",
    )
    first = messages.post_message(
        dispatch_id=dispatch_id,
        msg_type="result",
        payload={"complete": True, "text": "terminal fanout"},
        messages_dir=tmp_path / "messages",
        source={"node": "test", "adapter": "pytest", "transport": "journal"},
    )
    terminal_event_id = str(first["envelope"]["id"])
    messages.post_message(
        dispatch_id=dispatch_id,
        msg_type="advisory",
        payload={"text": "replace terminal carrier"},
        messages_dir=tmp_path / "messages",
        source={"node": "test", "adapter": "pytest", "transport": "controller"},
        replace_if=lambda envelope: envelope.get("id") == terminal_event_id,
    )

    rows = authority.read_all(
        """SELECT recipient_label, withdrawn_at FROM delivery_events
           WHERE event_uuid = ? ORDER BY recipient_label""",
        (terminal_event_id,),
    )
    assert [str(row["recipient_label"]) for row in rows] == [
        "first-controller",
        "second-controller",
    ]
    assert all(row["withdrawn_at"] is not None for row in rows)


def test_waiter_death_releases_kernel_witness_and_monitor_is_not_required(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    root, env = isolated
    child = _spawn_lock_holder(root, env)
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip().endswith(".lock")
        covered = wake.coverage_status(root, controller_label="wake-test")
        assert covered["covered"] is True
        assert covered["monitor"] == {"required": False, "state": "not-applicable"}
        child.kill()
        child.wait(timeout=3)
        assert wake.live_waiters(root, controller_label="wake-test") == []
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)


def test_live_waiters_distinguishes_deleted_ledger_from_genuine_zero(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    root, _env = isolated
    authority = journal.open_or_create_journal(root)
    claimed = authority.claim_or_renew_lease(
        "wake-test",
        principal={"principal_id": "deleted-ledger-test"},
    )
    assert claimed.committed and claimed.value is not None
    holder = wake.register_lease_holder(
        root,
        controller_label="wake-test",
        lease_nonce=claimed.value.nonce,
    )
    try:
        directory = wake.ledger_dir(root)
        assert directory.is_dir()
        shutil.rmtree(directory)
        assert wake.live_waiters(root, controller_label="wake-test") is None
        assert wake.coverage_status(root, controller_label="wake-test") == {
            "covered": False,
            "reason": "waiter-probe-unavailable",
            "waiters": [],
            "monitor": {"required": False, "state": "not-applicable"},
        }
    finally:
        holder.close()

    directory.mkdir(parents=True)
    assert wake.live_waiters(root, controller_label="wake-test") == []
    assert wake.coverage_status(root, controller_label="wake-test")["reason"] == (
        "no-live-waiter-lock"
    )


def test_wake_ledger_symlink_policy_is_symmetric_and_real_dir_scan_holds_fd(
    isolated: tuple[Path, dict[str, str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _env = isolated
    foreign_root = tmp_path / "foreign-project"
    foreign_root.mkdir()
    with wake.register_waiter(
        foreign_root,
        controller_label="wake-test",
        kind="listener",
    ):
        foreign_directory = wake.ledger_dir(foreign_root)
        directory = wake.ledger_dir(root)
        directory.parent.mkdir(parents=True, exist_ok=True)
        directory.symlink_to(foreign_directory, target_is_directory=True)
        assert directory.is_symlink()
        with pytest.raises(OSError):
            wake.register_waiter(
                root,
                controller_label="wake-test",
                kind="listener",
            )
        with pytest.raises(OSError):
            wake.register_lease_holder(
                root,
                controller_label="wake-test",
                lease_nonce="symlink-refusal-capability",
            )
        assert wake.live_waiters(root, controller_label="wake-test") is None
        assert wake.coverage_status(root, controller_label="wake-test")["reason"] == (
            "waiter-probe-unavailable"
        )

    directory.unlink()
    directory.mkdir()
    real_scandir = wake.os.scandir
    scandir_arguments: list[object] = []

    def audited_scandir(target: object):
        scandir_arguments.append(target)
        return real_scandir(target)

    with (
        wake.register_waiter(
            root,
            controller_label="wake-test",
            kind="listener",
        ),
        monkeypatch.context() as patch_context,
    ):
        patch_context.setattr(wake.os, "scandir", audited_scandir)
        assert len(wake.live_waiters(root, controller_label="wake-test") or []) == 1
    assert scandir_arguments and all(
        isinstance(argument, int) for argument in scandir_arguments
    )
    assert wake.live_waiters(root, controller_label="wake-test") == []


def test_entry_notice_distinguishes_probe_unavailable_from_offline(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    root, _env = isolated
    unavailable_stream = io.StringIO()
    unavailable = wake.check_tool_entry(
        root,
        controller_label="wake-test",
        controller_claimed=True,
        mail_bearing=True,
        stream=unavailable_stream,
    )
    assert unavailable["reason"] == "waiter-probe-unavailable"
    assert unavailable_stream.getvalue().startswith(
        "listener coverage UNKNOWN (probe unavailable); "
        "if you have no listener, start: "
    )
    assert "listener offline" not in unavailable_stream.getvalue()

    wake.ledger_dir(root).mkdir(parents=True)
    offline_stream = io.StringIO()
    offline = wake.check_tool_entry(
        root,
        controller_label="wake-test",
        controller_claimed=True,
        mail_bearing=True,
        stream=offline_stream,
    )
    assert offline["reason"] == "no-live-waiter-lock"
    assert offline_stream.getvalue().startswith("listener offline; start: ")
    assert "coverage UNKNOWN" not in offline_stream.getvalue()


def test_lockfile_content_never_claims_liveness(isolated: tuple[Path, dict[str, str]]) -> None:
    root, _env = isolated
    registration = wake.register_waiter(root, controller_label="wake-test", kind="listener")
    stale_path = registration.record.path
    registration.close()
    stale_path.write_text("alive=true\npid=1\n", encoding="utf-8")
    assert wake.live_waiters(root, controller_label="wake-test") == []
    assert not stale_path.exists()


def test_waiter_coverage_rejects_transient_fork_inheritance(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    root, env = isolated
    orphaned = root / "fork-child-orphaned"
    code = f"""
import os, sys, time
from pathlib import Path
sys.path.insert(0, {str(SCRIPTS)!r})
import goalflight_wake as w
r = w.register_waiter({str(root)!r}, controller_label='wake-test', kind='wait')
owner = os.getpid()
child = os.fork()
if child:
    print(f'{{r.record.path}}|{{child}}', flush=True)
    os._exit(0)
deadline = time.monotonic() + 3
while os.getppid() == owner and time.monotonic() < deadline:
    time.sleep(0.01)
Path({str(orphaned)!r}).write_text('orphaned')
time.sleep(60)
"""
    holder = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    forked_pid: int | None = None
    try:
        assert holder.stdout is not None
        path_text, raw_pid = holder.stdout.readline().strip().split("|", 1)
        forked_pid = int(raw_pid)
        deadline = time.monotonic() + 3
        while not orphaned.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert orphaned.exists(), "fork child did not outlive its recorded owner"
        # Sandboxed macOS may deny `ps` for a zombie that libproc no longer
        # exposes. UNKNOWN is deliberately the same fail-noisy direction.
        assert wake.goalflight_compat.pid_is_zombie(holder.pid) is not False
        assert wake.live_waiters(root, controller_label="wake-test") == []
        assert not Path(path_text).exists()
        holder.wait(timeout=3)
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=3)
        if forked_pid:
            with contextlib.suppress(ProcessLookupError):
                os.kill(forked_pid, signal.SIGKILL)


def test_waiter_descriptor_is_close_on_exec(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    root, _env = isolated
    registration = wake.register_waiter(
        root,
        controller_label="wake-test",
        kind="wait",
    )
    try:
        assert os.get_inheritable(registration._fd) is False
    finally:
        registration.close()


def test_controller_lease_lock_releases_on_sigkill(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    root, env = isolated
    nonce = uuid.uuid4().hex
    child = _spawn_lease_lock_holder(root, env, label="wake-test", nonce=nonce)
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip().endswith(".lock")
        assert wake.lease_holder_alive(
            root,
            controller_label="wake-test",
            lease_nonce=nonce,
        )
        child.kill()
        child.wait(timeout=3)
        assert not wake.lease_holder_alive(
            root,
            controller_label="wake-test",
            lease_nonce=nonce,
        )
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)


def test_deleted_live_lease_address_is_unknown_not_dead(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    root, env = isolated
    nonce = uuid.uuid4().hex
    child = _spawn_lease_lock_holder(root, env, label="wake-test", nonce=nonce)
    try:
        assert child.stdout is not None
        lock_path = Path(child.stdout.readline().strip())
        assert wake.lease_holder_alive(
            root,
            controller_label="wake-test",
            lease_nonce=nonce,
        ) is True
        shutil.rmtree(lock_path.parent)
        assert wake.lease_holder_alive(
            root,
            controller_label="wake-test",
            lease_nonce=nonce,
        ) is None
        lease_result = journal.open_or_create_journal(root).claim_or_renew_lease(
            "wake-test",
            principal={"principal_id": "ambient-incumbent"},
            nonce=nonce,
        )
        assert lease_result.committed and lease_result.value is not None
        liveness = sessions._lease_holder_liveness(lease_result.value)
        assert liveness is not None and liveness.alive is None
        refused = journal.Journal(root).claim_or_renew_lease(
            "wake-test",
            principal={"principal_id": "ambient-contender"},
            incumbent_liveness=liveness,
        )
        assert refused.cas_lost
        assert "label in use" in str(refused.reason)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)


def test_second_generation_holder_loses_well_known_lock(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    root, env = isolated
    nonce = uuid.uuid4().hex
    first = _spawn_lease_lock_holder(root, env, label="wake-test", nonce=nonce)
    second: subprocess.Popen[str] | None = None
    try:
        assert first.stdout is not None
        first_path = first.stdout.readline().strip()
        second = _spawn_lease_lock_holder(root, env, label="wake-test", nonce=nonce)
        _stdout, _stderr = second.communicate(timeout=3)
        assert second.returncode != 0
        assert wake.lease_holder_alive(
            root,
            controller_label="wake-test",
            lease_nonce=nonce,
        ) is True
        assert first_path.endswith(".lock")
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=3)


def test_scoped_status_wait_does_not_cover_controller_mail(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    root, env = isolated
    authority = journal.open_or_create_journal(root)
    claimed = authority.claim_or_renew_lease(
        "wake-test", principal={"principal_id": "wake-layer-test"}
    )
    assert claimed.committed and claimed.value is not None
    listener = subprocess.Popen(
        [
            sys.executable,
            str(SCRIPTS / "goalflight_messages.py"),
            "listen",
            "--project-root",
            str(root),
            "--controller-label",
            "wake-test",
            "--lease-nonce",
            claimed.value.nonce,
            "--poll-secs",
            "0.02",
            "--timeout-s",
            "10",
            "--json",
        ],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    long_wait = subprocess.Popen(
        [
            sys.executable,
            str(SCRIPTS / "goalflight_status.py"),
            "--project",
            str(root),
            "--wait",
            "not-yet-terminal",
            "--timeout-s",
            "10",
            "--poll-s",
            "0.05",
        ],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        kinds: set[str] = set()
        while time.monotonic() < deadline:
            kinds = {
                row.kind
                for row in (
                    wake.live_waiters(root, controller_label="wake-test") or []
                )
            }
            if kinds == {"listener", "wait"}:
                break
            time.sleep(0.05)
        assert kinds == {"listener", "wait"}
        covered = wake.coverage_status(root, controller_label="wake-test")
        assert covered["covered"] is True
        assert {row["kind"] for row in covered["waiters"]} == {"listener"}
        listener.kill()
        listener.communicate(timeout=3)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if {
                row.kind
                for row in (
                    wake.live_waiters(root, controller_label="wake-test") or []
                )
            } == {"wait"}:
                break
            time.sleep(0.02)
        assert wake.coverage_status(root, controller_label="wake-test")["covered"] is False
    finally:
        for process in (listener, long_wait):
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3)
    assert wake.live_waiters(root, controller_label="wake-test") == []


def test_wait_mail_watermark_does_not_take_the_journal_write_lock(
    isolated: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _env = isolated
    authority = journal.open_or_create_journal(root)
    claimed = authority.claim_or_renew_lease(
        "wake-test",
        principal={"principal_id": "fast-wait-reader-test"},
    )
    assert claimed.committed and claimed.value is not None
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_SESSION_ID", claimed.value.nonce)

    def reject_slow_constructor(*_args, **_kwargs):
        raise AssertionError("wait watermark used the write-locking Journal constructor")

    monkeypatch.setattr(journal.Journal, "__init__", reject_slow_constructor)

    reader = journal.Journal.open_reader(root)
    with pytest.raises(journal.JournalError, match="read-only journal client"):
        reader.write([])
    with reader._connect() as connection:
        with pytest.raises(journal.sqlite3.OperationalError, match="readonly"):
            connection.execute("CREATE TABLE forbidden_reader_write (value TEXT)")

    ambient = messages._ambient_claimed_controller(
        root,
        controller_label="wake-test",
        mail_bearing=True,
        require_live_holder=False,
    )
    assert ambient["claimed"] is True
    assert messages.controller_mail_summary(
        task_store_project_root=root,
        controller_label="wake-test",
    )["count"] == 0
    assert status._mail_watermark(str(root), ["wake-probe"]) == set()


def test_real_journal_mail_arrival_wakes_live_worker_wait_with_exit_three(
    isolated: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _env = isolated
    dispatch_id = "real-mail-wake"
    authority = journal.open_or_create_journal(root)
    claimed = authority.claim_or_renew_lease(
        "wake-test",
        principal={"principal_id": "real-mail-wake-controller"},
    )
    assert claimed.committed and claimed.value is not None
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_SESSION_ID", claimed.value.nonce)

    prepared = authority.prepare_attempt(dispatch_id)
    assert prepared.committed and prepared.value is not None
    started = authority.start_attempt(
        prepared.value.attempt_id,
        prepared.value.launch_token,
    )
    assert started.committed and started.value is not None
    running = authority.mark_attempt_running(
        started.value.attempt_id,
        started.value.launch_token,
        launch_epoch=started.value.launch_epoch,
        worker_instance={"pid": os.getpid(), "source": "mail-wake-probe"},
    )
    assert running.committed

    baseline_read = threading.Event()
    producer_errors: list[BaseException] = []
    real_watermark = status._mail_watermark

    def observed_watermark(*args, **kwargs):
        watermark = real_watermark(*args, **kwargs)
        baseline_read.set()
        return watermark

    monkeypatch.setattr(status, "_mail_watermark", observed_watermark)

    def post_after_arm() -> None:
        try:
            assert baseline_read.wait(timeout=2)
            messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="controller-notice",
                payload={"text": "wake the live worker wait"},
                messages_dir=messages.default_messages_dir(),
                source={"node": "test", "adapter": "pytest", "transport": "controller"},
                addressee=messages.controller_addressee(
                    "wake-test",
                    project_root=root,
                ),
            )
        except BaseException as exc:  # thread transports its failure to the test.
            producer_errors.append(exc)

    producer = threading.Thread(target=post_after_arm)
    producer.start()
    code = status._wait_for_dispatches_registered(
        [dispatch_id],
        project_root=str(root),
        timeout_s=3,
        poll_s=0.02,
        heartbeat_s=10,
    )
    producer.join(timeout=2)

    assert not producer_errors
    assert code == 3
    live_attempt = journal.Journal.open_reader(root).attempt_for_dispatch(dispatch_id)
    assert live_attempt is not None
    assert live_attempt.lifecycle_state == journal.ATTEMPT_RUNNING


def test_unclaimed_wait_wakes_on_mail_to_waited_dispatch_with_exit_three(
    isolated: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _env = isolated
    dispatch_id = "unclaimed-mail-wake"
    authority = journal.open_or_create_journal(root)
    assert authority.active_lease("wake-test") is None

    prepared = authority.prepare_attempt(dispatch_id)
    assert prepared.committed and prepared.value is not None
    started = authority.start_attempt(
        prepared.value.attempt_id,
        prepared.value.launch_token,
    )
    assert started.committed and started.value is not None
    running = authority.mark_attempt_running(
        started.value.attempt_id,
        started.value.launch_token,
        launch_epoch=started.value.launch_epoch,
        worker_instance={"pid": os.getpid(), "source": "unclaimed-mail-wake-probe"},
    )
    assert running.committed

    baseline_read = threading.Event()
    producer_errors: list[BaseException] = []
    real_watermark = status._mail_watermark

    def observed_watermark(*args, **kwargs):
        watermark = real_watermark(*args, **kwargs)
        baseline_read.set()
        return watermark

    monkeypatch.setattr(status, "_mail_watermark", observed_watermark)

    def post_after_arm() -> None:
        try:
            assert baseline_read.wait(timeout=2)
            result = messages.post_message(
                dispatch_id=dispatch_id,
                msg_type="controller-notice",
                payload={"text": "wake the unclaimed fixed-set wait"},
                messages_dir=messages.default_messages_dir(),
                source={"node": "test", "adapter": "pytest", "transport": "controller"},
                deliver_to_worker=True,
            )
            assert result["recorded"] is True
        except BaseException as exc:  # thread transports its failure to the test.
            producer_errors.append(exc)

    producer = threading.Thread(target=post_after_arm)
    producer.start()
    code = status._wait_for_dispatches_registered(
        [dispatch_id],
        project_root=str(root),
        timeout_s=3,
        poll_s=0.02,
        heartbeat_s=10,
    )
    producer.join(timeout=2)

    assert not producer_errors
    assert code == 3
    assert authority.delivery_event_watermark(stream_ids=[dispatch_id]) == set()
    live_attempt = journal.Journal.open_reader(root).attempt_for_dispatch(dispatch_id)
    assert live_attempt is not None
    assert live_attempt.lifecycle_state == journal.ATTEMPT_RUNNING


def test_deleted_cursor_token_cli_surface_does_not_displace_healthy_listener(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    root, env = isolated
    authority = journal.open_or_create_journal(root)
    claimed = authority.claim_or_renew_lease(
        "wake-test", principal={"principal_id": "bad-token-test"}
    )
    assert claimed.committed and claimed.value is not None
    command = [
        sys.executable,
        str(SCRIPTS / "goalflight_messages.py"),
        "listen",
        "--project-root",
        str(root),
        "--controller-label",
        "wake-test",
        "--lease-nonce",
        claimed.value.nonce,
        "--poll-secs",
        "0.02",
        "--timeout-s",
        "10",
        "--json",
    ]
    healthy = subprocess.Popen(
        command,
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        coverage = None
        while time.monotonic() < deadline:
            coverage = authority.active_coverage("wake-test")
            if coverage and wake.live_waiters(root, controller_label="wake-test"):
                break
            time.sleep(0.05)
        assert coverage is not None
        coverage_id = coverage["coverage_id"]
        bad = subprocess.run(
            [*command, "--cursor-token", "not-a-valid-cursor-token"],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        assert bad.returncode == 2
        assert "unrecognized arguments: --cursor-token" in bad.stderr
        current = authority.active_coverage("wake-test")
        assert current is not None and current["coverage_id"] == coverage_id
        assert healthy.poll() is None
        assert wake.coverage_status(root, controller_label="wake-test")["covered"] is True
    finally:
        if healthy.poll() is None:
            healthy.kill()
        healthy.communicate(timeout=3)


def test_failed_waiter_registration_does_not_displace_healthy_listener(
    isolated: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    root, env = isolated
    authority = journal.open_or_create_journal(root)
    claimed = authority.claim_or_renew_lease(
        "wake-test", principal={"principal_id": "registration-order-test"}
    )
    assert claimed.committed and claimed.value is not None
    command = [
        sys.executable,
        str(SCRIPTS / "goalflight_messages.py"),
        "listen",
        "--project-root",
        str(root),
        "--controller-label",
        "wake-test",
        "--lease-nonce",
        claimed.value.nonce,
        "--poll-secs",
        "0.02",
        "--timeout-s",
        "10",
        "--json",
    ]
    healthy = subprocess.Popen(
        command,
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        original_coverage = None
        while time.monotonic() < deadline:
            original_coverage = authority.active_coverage("wake-test")
            if original_coverage and wake.live_waiters(root, controller_label="wake-test"):
                break
            time.sleep(0.05)
        assert original_coverage is not None

        blocked_ledger = tmp_path / "not-a-directory"
        blocked_ledger.write_text("ledger path collision\n", encoding="utf-8")
        replacement_env = dict(env)
        replacement_env["GOALFLIGHT_WAKE_LEDGER_DIR"] = str(blocked_ledger)
        replacement = subprocess.run(
            command,
            cwd=root,
            env=replacement_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        assert replacement.returncode == 2
        assert "wake ledger registration failed" in replacement.stderr
        current = authority.active_coverage("wake-test")
        assert current is not None
        assert current["coverage_id"] == original_coverage["coverage_id"]
        assert healthy.poll() is None
        assert wake.coverage_status(root, controller_label="wake-test")["covered"] is True
    finally:
        if healthy.poll() is None:
            healthy.kill()
        healthy.communicate(timeout=3)


def _entry_commands(root: Path, tmp_path: Path) -> list[tuple[list[str], bool]]:
    return [
        ([sys.executable, str(SCRIPTS / "goalflight_status.py"), "--project", str(root), "--json"], True),
        ([sys.executable, str(SCRIPTS / "goalflight_dispatch.py"), "--stats", "1d", "--json"], True),
        ([sys.executable, str(ROOT / "goalflight_task.py"), "--project-root", str(root), "status", "--json"], True),
        (
            [
                sys.executable,
                str(SCRIPTS / "goalflight_messages.py"),
                "--messages-dir",
                str(tmp_path / "messages"),
                "--fleet-dir",
                str(tmp_path / "fleet"),
                "status",
            ],
            False,
        ),
        ([sys.executable, str(SCRIPTS / "goalflight_session_status.py"), "--project-root", str(root), "--json"], True),
        ([sys.executable, str(SCRIPTS / "goalflight_journal.py"), "--project-root", str(root), "inspect"], True),
    ]


def test_unclaimed_cli_entries_stay_quiet_and_claimed_mail_entries_warn_once(
    isolated: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    root, env = isolated
    authority = journal.open_or_create_journal(root)
    commands = _entry_commands(root, tmp_path)
    for command, _mail_bearing in commands:
        result = subprocess.run(
            command,
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert "listener offline; start: " not in result.stderr, (
            command,
            result.returncode,
            result.stdout,
            result.stderr,
        )
    assert authority.active_lease("wake-test") is None

    incumbent = authority.claim_or_renew_lease(
        "wake-test", principal={"principal_id": "runnable-command-incumbent"}
    )
    assert incumbent.committed and incumbent.value is not None
    env["GOALFLIGHT_CONTROLLER_SESSION_ID"] = incumbent.value.nonce
    lines: list[str] = []
    holder = wake.register_lease_holder(
        root,
        controller_label="wake-test",
        lease_nonce=incumbent.value.nonce,
    )
    try:
        for command, mail_bearing in commands:
            result = subprocess.run(
                command,
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            offline = [
                line
                for line in result.stderr.splitlines()
                if line.startswith("listener offline; start: ")
            ]
            assert len(offline) == int(mail_bearing), (
                command,
                result.returncode,
                result.stdout,
                result.stderr,
            )
            lines.extend(offline)
    finally:
        holder.close()

    argv = shlex.split(lines[0].split("start: ", 1)[1])
    runnable_holder = wake.register_lease_holder(
        root,
        controller_label="wake-test",
        lease_nonce=incumbent.value.nonce,
    )
    try:
        runnable = subprocess.run(
            [*argv, "--timeout-s", "0.05", "--poll-secs", "0.01", "--json"],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    finally:
        runnable_holder.close()
    assert runnable.returncode in {0, 1}, runnable.stderr
    payload = json.loads(runnable.stdout.splitlines()[-1])
    assert payload["reason"] == "timeout"


def test_one_shot_controller_role_without_session_beacon_never_auto_claims() -> None:
    assert sessions._auto_claim_refusal_reason(
        role="controller",
        has_session_beacon=False,
        worker_dispatch=False,
        session_entry=True,
    ) == "missing_session_beacon"
    assert sessions._auto_claim_refusal_reason(
        role="controller",
        has_session_beacon=True,
        worker_dispatch=False,
        session_entry=True,
    ) is None
    assert sessions._auto_claim_refusal_reason(
        role="controller",
        has_session_beacon=True,
        worker_dispatch=False,
        session_entry=False,
    ) == "one_shot_cli_does_not_claim"


def test_ambient_capability_still_gets_mail_fallback_when_holder_is_unknown(
    isolated: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _env = isolated
    authority = journal.open_or_create_journal(root)
    claimed = authority.claim_or_renew_lease(
        "wake-test",
        principal={"principal_id": "constructed-ambient"},
    )
    assert claimed.committed and claimed.value is not None
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_SESSION_ID", claimed.value.nonce)
    strict = messages._ambient_claimed_controller(
        root,
        controller_label="wake-test",
        mail_bearing=True,
    )
    assert strict["claimed"] is False
    assert strict["reason"] == "controller-lease-holder-unknown"
    fallback = messages._ambient_claimed_controller(
        root,
        controller_label="wake-test",
        mail_bearing=True,
        require_live_holder=False,
    )
    assert fallback["claimed"] is True
    assert fallback["holder_alive"] is None

    with wake.register_waiter(
        root,
        controller_label="wake-test",
        kind="wait",
    ):
        stream = io.StringIO()
        result = messages.emit_wake_entry_notice(
            project_root=root,
            controller_label="wake-test",
            stream=stream,
        )
    assert result["covered"] is False
    assert stream.getvalue().startswith("listener offline; start: ")


def test_one_shot_cli_entries_with_a_real_beacon_still_do_not_claim(
    isolated: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    root, env = isolated
    authority = journal.open_or_create_journal(root)
    host = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    env["GOALFLIGHT_CONTROLLER_PID"] = str(host.pid)
    try:
        for command, _mail_bearing in _entry_commands(root, tmp_path):
            result = subprocess.run(
                command,
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            assert result.returncode == 0, (command, result.stdout, result.stderr)
        assert authority.active_lease("wake-test") is None
    finally:
        host.kill()
        host.wait(timeout=3)


def test_worker_non_mail_and_mismatched_capability_entries_stay_quiet(
    isolated: tuple[Path, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _env = isolated
    authority = journal.open_or_create_journal(root)
    claimed = authority.claim_or_renew_lease(
        "wake-test", principal={"principal_id": "entry-boundary-test"}
    )
    assert claimed.committed and claimed.value is not None

    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_SESSION_ID", "stale-capability")
    stream = io.StringIO()
    mismatched = messages.emit_wake_entry_notice(project_root=root, stream=stream)
    assert mismatched["reason"] == "no-ambient-claimed-controller"
    assert stream.getvalue() == ""

    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_SESSION_ID", claimed.value.nonce)
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_ID", "worker-entry")
    worker_stream = io.StringIO()
    worker = messages.emit_wake_entry_notice(project_root=root, stream=worker_stream)
    assert worker["reason"] == "no-ambient-claimed-controller"
    assert worker_stream.getvalue() == ""

    monkeypatch.delenv("GOALFLIGHT_DISPATCH_ID")
    non_mail_stream = io.StringIO()
    non_mail = messages.emit_wake_entry_notice(
        project_root=root,
        mail_bearing=False,
        stream=non_mail_stream,
    )
    assert non_mail["reason"] == "not-mail-bearing"
    assert non_mail_stream.getvalue() == ""


def test_live_waiter_coverage_skips_notice_and_poll(
    isolated: tuple[Path, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _env = isolated
    authority = journal.open_or_create_journal(root)
    claimed = authority.claim_or_renew_lease(
        "wake-test", principal={"principal_id": "covered-entry-test"}
    )
    assert claimed.committed and claimed.value is not None
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_SESSION_ID", claimed.value.nonce)
    monkeypatch.setenv("GOALFLIGHT_WAKE_ENTRY_POLL_S", "0.5")

    lease_holder = wake.register_lease_holder(
        root,
        controller_label="wake-test",
        lease_nonce=claimed.value.nonce,
    )
    with wake.register_waiter(root, controller_label="wake-test", kind="listener"):
        stream = io.StringIO()
        result = messages.emit_wake_entry_notice(project_root=root, stream=stream)
    lease_holder.close()

    assert result["covered"] is True
    assert result["reason"] == "held-flock"
    assert stream.getvalue() == ""


def test_offline_entry_poll_surfaces_real_mail_without_listener(
    isolated: tuple[Path, dict[str, str]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, _env = isolated
    monkeypatch.setenv("GOALFLIGHT_WAKE_ENTRY_POLL_S", "0.5")
    authority = journal.open_or_create_journal(root)
    claimed = authority.claim_or_renew_lease(
        "wake-test", principal={"principal_id": "poll-test"}
    )
    assert claimed.committed and claimed.value is not None
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_SESSION_ID", claimed.value.nonce)
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_LABEL", "different-ambient-label")

    def produce() -> None:
        time.sleep(0.05)
        messages.post_message(
            dispatch_id="poll-fallback-producer",
            msg_type="controller-notice",
            payload={"text": "wake through bounded entry poll"},
            messages_dir=tmp_path / "messages",
            source={"node": "test", "adapter": "test", "transport": "controller"},
            addressee=messages.controller_addressee("wake-test", project_root=root),
        )

    producer = threading.Thread(target=produce)
    producer.start()
    lease_holder = wake.register_lease_holder(
        root,
        controller_label="wake-test",
        lease_nonce=claimed.value.nonce,
    )
    stream = io.StringIO()
    result = messages.emit_wake_entry_notice(
        project_root=root,
        controller_label="wake-test",
        owned_dispatch_ids=set(),
        messages_dir=tmp_path / "messages",
        stream=stream,
    )
    lease_holder.close()
    producer.join(timeout=3)
    assert not producer.is_alive()
    assert result["covered"] is False
    assert "1 new mail; peek:" in stream.getvalue()


def test_attention_generation_noise_collapses_to_one_open_item(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    root, _env = isolated
    authority = journal.open_or_create_journal(root)
    generations: list[int] = []
    for index in range(3):
        claimed = authority.claim_or_renew_lease(
            "wake-test",
            principal={"principal_id": f"attention-{index}"},
            takeover=index > 0,
        )
        assert claimed.committed and claimed.value is not None
        generations.append(claimed.value.generation)

    def seed(connection) -> None:
        now = journal.utc_now()
        for seq, generation in enumerate(generations, 1):
            item_id = uuid.uuid4().hex
            payload = {
                "item_id": item_id,
                "type": "orphaned_controller_work",
                "source_label": "wake-test",
                "source_generation": generation,
                "text": f"controller lease wake-test generation {generation} needs reassignment",
            }
            connection.execute(
                """
                INSERT INTO attention_items (
                    item_id, project_root, item_type, state, source_label,
                    source_generation, trigger_side, reason, payload_json,
                    wake_class, created_at
                ) VALUES (?, ?, 'orphaned_controller_work', 'OPEN', ?, ?,
                          'horizon', 'stale-lease', ?, 'waking', ?)
                """,
                (item_id, str(root.resolve()), "wake-test", generation, json.dumps(payload), now),
            )
            connection.execute(
                """
                INSERT INTO delivery_events (
                    project_root, recipient_label, origin_node, event_uuid,
                    stream_id, stream_seq, carrier_path, event_type,
                    wake_class, created_at, projected_at
                ) VALUES (?, '*', 'journal', ?, 'attention', ?, ?,
                          'controller_attention', 'waking', ?, ?)
                """,
                (str(root.resolve()), item_id, seq, f"journal:attention:{item_id}", now, now),
            )

    seeded = authority._domain_write(seed)
    assert seeded.committed
    replacement = authority.claim_or_renew_lease(
        "wake-test",
        principal={"principal_id": "attention-replacement"},
        takeover=True,
    )
    assert replacement.committed
    open_items = authority.attention_items()
    assert len(open_items) == 1
    assert open_items[0]["source_generation"] == generations[-1]
    assert len(authority.attention_items(state="RESOLVED")) == 2
    withdrawn = authority.read_all(
        "SELECT withdrawn_at FROM delivery_events WHERE event_type = 'controller_attention'"
    )
    assert sum(row["withdrawn_at"] is not None for row in withdrawn) == 2


def test_lease_horizon_outlives_wait_heartbeat_and_hourly_watchdog() -> None:
    assert journal.DEFAULT_LEASE_HORIZON_S >= 2 * 60 * 60
    assert journal.DEFAULT_LEASE_HORIZON_S > status._WAIT_HEARTBEAT_S
