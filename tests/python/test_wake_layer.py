"""Held-lock wake coverage, poll fallback, and universal entry contracts."""

from __future__ import annotations

import contextlib
import errno
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

from machine_isolation import AMBIENT_IDENTITY_ENV, isolated_machine_env, wait_until


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


_WAKE_DELIVERY_BUDGET_S = 5.0
_COMPLETION_MAX_IDLE_S = 20.0
# Communicate/wait harness must outlive the listener --timeout-s. Matching
# them makes communicate raise TimeoutExpired while the listener is still
# winding down its own timeout under load.
_LISTENER_PROCESS_TIMEOUT_S = 60.0
_COMPLETION_HARNESS_TIMEOUT_S = 90.0
_LISTENER_ARM_HARNESS_TIMEOUT_S = 60.0


def isolated_env(tmp_path: Path, *, label: str = "wake-test") -> dict[str, str]:
    env = dict(os.environ)
    # Advertised re-arm commands start with `python3`. Prefer the suite
    # interpreter so PATH does not resolve to an unrelated system Python.
    python_dir = str(Path(sys.executable).resolve().parent)
    env["PATH"] = python_dir + os.pathsep + env.get("PATH", "")
    for key in AMBIENT_IDENTITY_ENV:
        env.pop(key, None)
    env.pop("GOALFLIGHT_WAKE_LEDGER", None)
    env.update(isolated_machine_env(tmp_path))
    env.update(
        {
            "GOALFLIGHT_ROOT": str(ROOT),
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
    for key, value in env.items():
        if key.startswith("GOAL") or key in {"PYTHONUNBUFFERED", "PATH"}:
            monkeypatch.setenv(key, value)
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
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
    *,
    forced: bool = False,
) -> list[str]:
    command = [
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
        str(_COMPLETION_MAX_IDLE_S),
        "--foreground",
    ]
    if forced:
        command.append("--unregistered-forced")
    return [
        *command,
        "--",
        sys.executable,
        "-c",
        f"print('COMPLETE: {dispatch_id} — done', flush=True)",
    ]


def _wake_delivery_elapsed(authority: journal.Journal, dispatch_id: str) -> float:
    """Measure the product wake path, excluding process cold-start latency."""
    rows = authority.read_all(
        """SELECT created_at, projected_at FROM delivery_events
           WHERE stream_id = ? AND wake_class = 'waking'""",
        (dispatch_id,),
    )
    assert rows, f"no waking delivery projected for {dispatch_id}"
    elapsed: list[float] = []
    for row in rows:
        created_at = journal._parse_utc(row["created_at"])
        projected_at = journal._parse_utc(row["projected_at"])
        assert created_at is not None and projected_at is not None, dict(row)
        delta = (projected_at - created_at).total_seconds()
        assert delta >= 0, dict(row)
        elapsed.append(delta)
    return max(elapsed)


def _listener_command(
    root: Path,
    tmp_path: Path,
    *,
    label: str,
    nonce: str,
    slots: int | None = None,
    timeout_s: float = 8,
    report_pending: bool = True,
) -> list[str]:
    """Build a `listen` invocation.

    `report_pending=False` selects the legacy path, where mail already pending
    at arm time rings the doorbell. Tests that seed a backlog BEFORE arming and
    then expect a ring need it: the default reports an arm-time backlog once and
    then waits only for newer events, so pre-seeded mail no longer rings.
    """
    command = [
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
        str(timeout_s),
        "--json",
    ]
    if slots is not None:
        command.extend(["--listener-slots", str(slots)])
    if not report_pending:
        command.append("--no-report-pending")
    return command


def _wait_for_listener(
    root: Path,
    label: str,
    listener: subprocess.Popen[str],
) -> None:
    """Wait on the kernel slot lock, then journal coverage.

    Slot lock is taken before the arm-time peek. Coverage is published after
    that peek, which is the real 'doorbell is waiting for new events' boundary.
    Slow-poll coverage so the parent does not hammer sqlite.
    """

    def _locked() -> bool:
        waiters = wake.live_waiters(root, controller_label=label) or []
        return any(row.pid == listener.pid for row in waiters)

    wait_until(
        _locked,
        timeout_s=_LISTENER_ARM_HARNESS_TIMEOUT_S,
        interval_s=0.02,
        message=f"listener pid={listener.pid} slot lock for {label}",
    )

    def _covered() -> bool:
        try:
            coverage = journal.Journal(root, retry_budget_s=5.0).active_coverage(label)
        except journal.JournalBusy:
            return False
        return coverage is not None and int(coverage.get("pid") or 0) == listener.pid

    wait_until(
        _covered,
        timeout_s=_LISTENER_ARM_HARNESS_TIMEOUT_S,
        interval_s=0.1,
        message=f"listener pid={listener.pid} journal coverage for {label}",
    )


def _hold_claimed_lease(
    root: Path, lease: journal.LeaseIdentity
) -> wake.LeaseHolderRegistration:
    """Keep the controller lease lock live while a test arms listen/follow."""
    holder = wake.register_lease_holder(
        root,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    )
    holder.__enter__()
    return holder


def _configured_pool_size() -> int:
    return wake.listener_slot_count()


def _assert_arbitration_pool_legal(slots: int) -> int:
    """Explicit N-listener arbitration fixture; depth is a target, not a cap."""
    assert slots >= 1, f"arbitration fixture of {slots} is not a positive depth"
    return slots


# Arbitration fixtures wait up to EXIT + LIVE + EXIT seconds before the
# "N of M exited" check. A listener --timeout-s equal to that sum expires
# during the second exit-wait under load and is counted as a surplus exit
# (the t-282 3-where-2 flake). The fixture must outlive the wait budget.
_ARBITRATION_EXIT_WAIT_S = 20.0
_ARBITRATION_LIVE_WAIT_S = 60.0
_ARBITRATION_LISTENER_TIMEOUT_S = 180.0
assert _ARBITRATION_LISTENER_TIMEOUT_S > (
    2 * _ARBITRATION_EXIT_WAIT_S + _ARBITRATION_LIVE_WAIT_S
)


def _wait_for_listener_count(
    root: Path,
    *,
    label: str,
    count: int,
    timeout_s: float = _ARBITRATION_LIVE_WAIT_S,
) -> list[wake.WaiterRecord]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        waiters = wake.live_waiters(root, controller_label=label) or []
        if len(waiters) == count:
            return waiters
        time.sleep(0.02)
    raise AssertionError(f"listener pool for {label} never reached n={count}")


def _post_notice(
    root: Path,
    tmp_path: Path,
    *,
    label: str,
    dispatch_id: str,
    text: str,
    source: dict[str, str] | None = None,
    author_capability: str | None = None,
) -> dict:
    return messages.post_message(
        dispatch_id=dispatch_id,
        msg_type="controller-notice",
        payload={"text": text},
        messages_dir=tmp_path / "messages",
        source=source
        or {"node": "peer", "adapter": "pytest", "transport": "controller"},
        author_capability=author_capability,
        addressee=messages.controller_addressee(label, project_root=root),
    )["envelope"]


def _wait_for_exited_count(
    processes: list[subprocess.Popen[str]],
    count: int,
    *,
    timeout_s: float = _ARBITRATION_EXIT_WAIT_S,
) -> list[subprocess.Popen[str]]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        exited = [process for process in processes if process.poll() is not None]
        if len(exited) == count:
            return exited
        if len(exited) > count:
            break
        time.sleep(0.02)
    return [process for process in processes if process.poll() is not None]


def _pool_exit_codes(processes: list[subprocess.Popen[str]]) -> list[int | None]:
    return [process.poll() for process in processes]


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
    # One doorbell is the whole pool for this test. Default target 4 makes
    # dispatch treat the armed waiter as reserve-down and exit 64.
    env["GOALFLIGHT_LISTENER_SLOTS"] = "1"
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
                    slots=1,
                    timeout_s=_LISTENER_PROCESS_TIMEOUT_S,
                ),
                cwd=root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            completed: subprocess.Popen[str] | None = None
            try:
                _wait_for_listener(root, "wake-test", listener)
                completed = subprocess.Popen(
                    _completion_dispatch_command(root, tmp_path, dispatch_id),
                    cwd=root,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                completed_stdout, completed_stderr = completed.communicate(
                    timeout=_COMPLETION_HARNESS_TIMEOUT_S
                )
                assert completed.returncode == 0, (completed_stdout, completed_stderr)
                listener_stdout, listener_stderr = listener.communicate(
                    timeout=_COMPLETION_HARNESS_TIMEOUT_S
                )
                assert listener.returncode == 0, listener_stderr
                assert json.loads(listener_stdout)["reason"] == "event"
                elapsed = _wake_delivery_elapsed(
                    journal.Journal(root, retry_budget_s=10.0), dispatch_id
                )
                measurements.append(elapsed)
                record = json.loads(ledger.record_path(dispatch_id, create=False).read_text())
                assert record["controller_label"] == "wake-test", (
                    record,
                    completed_stdout,
                    completed_stderr,
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
                if completed is not None and completed.poll() is None:
                    completed.kill()
                    completed.communicate(timeout=3)
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
    lease_holder = _hold_claimed_lease(root, first.value)
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
            slots=1,
            timeout_s=_LISTENER_PROCESS_TIMEOUT_S,
        ),
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    completed: subprocess.Popen[str] | None = None
    try:
        _wait_for_listener(root, "wake-test", listener)
        completed = subprocess.Popen(
            _completion_dispatch_command(
                root,
                tmp_path,
                dispatch_id,
                forced=True,
            ),
            cwd=root,
            env=dispatch_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        completed_stdout, completed_stderr = completed.communicate(
            timeout=_COMPLETION_HARNESS_TIMEOUT_S
        )
        assert completed.returncode == 0, (completed_stdout, completed_stderr)
        listener_stdout, listener_stderr = listener.communicate(
            timeout=_COMPLETION_HARNESS_TIMEOUT_S
        )
        assert listener.returncode == 0, listener_stderr
        assert json.loads(listener_stdout)["reason"] == "event"
        elapsed = _wake_delivery_elapsed(
            journal.Journal(root, retry_budget_s=10.0), dispatch_id
        )
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
        lease_holder.close()
        if listener.poll() is None:
            listener.kill()
            listener.communicate(timeout=3)
        if completed is not None and completed.poll() is None:
            completed.kill()
            completed.communicate(timeout=3)


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


def test_listener_pool_one_event_pops_exactly_one_real_process(
    isolated: tuple[Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    root, env = isolated
    authority = journal.open_or_create_journal(root)
    claimed = authority.claim_or_renew_lease(
        "wake-test", principal={"principal_id": "pool-pop-one"}
    )
    assert claimed.committed and claimed.value is not None
    lease_holder = _hold_claimed_lease(root, claimed.value)
    # Deliberate three-listener arbitration: one event must pop exactly one.
    arbitration_slots = _assert_arbitration_pool_legal(3)
    processes = [
        subprocess.Popen(
            _listener_command(
                root,
                tmp_path,
                label="wake-test",
                nonce=claimed.value.nonce,
                slots=arbitration_slots,
                timeout_s=_ARBITRATION_LISTENER_TIMEOUT_S,
            ),
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _index in range(arbitration_slots)
    ]
    try:
        waiters = _wait_for_listener_count(
            root, label="wake-test", count=arbitration_slots
        )
        assert len({row.pid for row in waiters}) == arbitration_slots
        assert wake.coverage_status(root, controller_label="wake-test")[
            "live_waiters"
        ] == arbitration_slots
        slot_names = {
            path.name
            for path in wake.ledger_dir(root).iterdir()
            if path.name.startswith("listener-slot-v1.")
        }
        assert len(slot_names) == arbitration_slots
        assert all(
            any(name.endswith(f"listener-slot-{index}.lock") for name in slot_names)
            for index in range(arbitration_slots)
        )

        _post_notice(
            root,
            tmp_path,
            label="wake-test",
            dispatch_id="pool-one-event",
            text="one event",
        )
        exited = _wait_for_exited_count(processes, 1)
        assert len(exited) == 1
        stdout, stderr = exited[0].communicate(timeout=3)
        assert exited[0].returncode == 0, stderr
        assert json.loads(stdout)["kind"] == "ring"
        assert all(process.poll() is None for process in processes if process not in exited)
        remaining = arbitration_slots - 1
        assert len(_wait_for_listener_count(root, label="wake-test", count=remaining)) == remaining

        # Mutation pair for the ring-stamp comparison: equality must suppress
        # duplicate pops, while any cursor change makes the stamp stale.
        assert wake._ring_stamp_needs_claim(None, 7) is True
        assert wake._ring_stamp_needs_claim(7, 7) is False
        assert wake._ring_stamp_needs_claim(7, 8) is True
        inverted_compare = lambda observed, cursor: observed == cursor
        assert inverted_compare(7, 7) is True
        assert inverted_compare(7, 8) is False
    finally:
        lease_holder.close()
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3)


def test_malformed_ring_stamp_is_quarantined_and_next_observation_recovers(
    isolated: tuple[Path, dict[str, str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _env = isolated
    stamp = wake._ring_stamp_path(root, controller_label="wake-test")
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_bytes(b"17-torn\n")

    # The pre-fix/remove-recovery mutation raises here on every observation,
    # which makes each listener exit 2. Production quarantines once and rings.
    assert wake.claim_ring(
        root,
        controller_label="wake-test",
        cursor_version=18,
    ) is True
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    assert "listener ring stamp quarantined as " in captured.err
    corrupt = list(stamp.parent.glob(f"{stamp.name}.corrupt-*"))
    assert len(corrupt) == 1
    assert corrupt[0].read_bytes() == b"17-torn\n"
    assert stamp.read_text(encoding="ascii") == "18\n"

    assert wake.claim_ring(
        root,
        controller_label="wake-test",
        cursor_version=18,
    ) is False
    assert capsys.readouterr().err == ""


def test_failed_pending_report_claim_is_exactly_recoverable(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    root, _env = isolated
    positions = {"stream-a": 3, "stream-b": 8}
    assert wake.claim_pending_report(
        root,
        controller_label="wake-test",
        lease_nonce="recoverable-pending-report",
        positions=positions,
    )
    assert wake.pending_report_high_water(
        root,
        controller_label="wake-test",
        lease_nonce="recoverable-pending-report",
    ) == positions

    assert not wake.release_pending_report_claim(
        root,
        controller_label="wake-test",
        lease_nonce="recoverable-pending-report",
        positions={"stream-a": 4, "stream-b": 8},
    ), "a mismatched reporter must not erase the durable claim"
    assert wake.release_pending_report_claim(
        root,
        controller_label="wake-test",
        lease_nonce="recoverable-pending-report",
        positions=positions,
    )
    assert wake.pending_report_high_water(
        root,
        controller_label="wake-test",
        lease_nonce="recoverable-pending-report",
    ) is None
    assert wake.claim_pending_report(
        root,
        controller_label="wake-test",
        lease_nonce="recoverable-pending-report",
        positions=positions,
    ), "a replacement arm must be able to report after delivery rollback"


def test_malformed_ring_stamp_does_not_trap_listener_in_exit_two_loop(
    isolated: tuple[Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    root, env = isolated
    authority = journal.open_or_create_journal(root)
    claimed = authority.claim_or_renew_lease(
        "wake-test", principal={"principal_id": "ring-stamp-listener-recovery"}
    )
    assert claimed.committed and claimed.value is not None
    lease_holder = _hold_claimed_lease(root, claimed.value)
    try:
        _post_notice(
            root,
            tmp_path,
            label="wake-test",
            dispatch_id="ring-stamp-recovery",
            text="ring after torn stamp",
        )
        stamp = wake._ring_stamp_path(root, controller_label="wake-test")
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_bytes(b"nonnumeric-torn-write\n")

        first = subprocess.run(
            _listener_command(
                root,
                tmp_path,
                label="wake-test",
                nonce=claimed.value.nonce,
                slots=1,
                timeout_s=2,
                report_pending=False,
            ),
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert first.returncode == 0, (first.stdout, first.stderr)
        recovery_lines = [
            line for line in first.stderr.splitlines() if "listener ring stamp" in line
        ]
        assert len(recovery_lines) == 1
        assert "listener ring stamp quarantined as " in recovery_lines[0]
        assert json.loads(first.stdout)["kind"] == "ring"

        # The same still-pending cursor version was stamped by the first listener.
        # A re-arm therefore times out cleanly instead of repeating exit 2 forever.
        second = subprocess.run(
            _listener_command(
                root,
                tmp_path,
                label="wake-test",
                nonce=claimed.value.nonce,
                slots=1,
                timeout_s=0.1,
                report_pending=False,
            ),
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert second.returncode == 1, (second.stdout, second.stderr)
        assert "ring stamp" not in second.stderr
        assert json.loads(second.stdout)["reason"] == "timeout"
    finally:
        lease_holder.close()


def test_listener_pool_defaults_to_the_configured_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOALFLIGHT_LISTENER_SLOTS", raising=False)
    assert wake.listener_slot_count() == wake.DEFAULT_LISTENER_SLOTS
    assert _configured_pool_size() == wake.DEFAULT_LISTENER_SLOTS
    monkeypatch.setenv("GOALFLIGHT_LISTENER_SLOTS", "3")
    assert wake.listener_slot_count() == 3
    assert wake.listener_slot_count(40) == 40
    with pytest.raises(ValueError, match="at least 1"):
        wake.listener_slot_count(0)
    assert not hasattr(wake, "MAX_LISTENER_SLOTS")
    assert not hasattr(wake, "ListenerSlotsFull")


def test_listener_reserve_hint_prints_one_command_per_missing_slot() -> None:
    target = wake.DEFAULT_LISTENER_SLOTS
    command = "python3 scripts/goalflight_messages.py listen --report-pending"
    assert wake.listener_reserve_hint(0, target, command) == (
        f"listener pool n=0; start: {command}"
    )
    one_live = wake.listener_reserve_hint(1, target, command)
    assert one_live.startswith(
        f"listener pool n=1/{target} — reserve down; re-arm: {command}"
    )
    assert one_live.count(command) == max(0, target - 1)
    # An almost-full pool is deliberately SILENT: one missing slot is not
    # news (operator ruling 2026-08-16). Only at or below the low-water mark
    # does the hint speak.
    if target >= 2:
        assert wake.listener_reserve_hint(target - 1, target, command) == ""
        low = wake.listener_low_water(target)
        at_low = wake.listener_reserve_hint(low, target, command)
        assert at_low.startswith(f"listener pool n={low}/{target}")
        assert at_low.count(command) == target - low


def test_entry_hint_grades_crashed_pool_member_and_full_pool_is_silent(
    isolated: tuple[Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    root, env = isolated
    authority = journal.open_or_create_journal(root)
    claimed = authority.claim_or_renew_lease(
        "wake-test", principal={"principal_id": "pool-depth-hint"}
    )
    assert claimed.committed and claimed.value is not None
    lease_holder = _hold_claimed_lease(root, claimed.value)
    command = _listener_command(
        root,
        tmp_path,
        label="wake-test",
        nonce=claimed.value.nonce,
        slots=wake.DEFAULT_LISTENER_SLOTS,
        timeout_s=_ARBITRATION_LISTENER_TIMEOUT_S,
    )
    processes = [
        subprocess.Popen(
            command,
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _index in range(wake.DEFAULT_LISTENER_SLOTS)
    ]
    try:
        _wait_for_listener_count(root, label="wake-test", count=wake.DEFAULT_LISTENER_SLOTS)
        full_stream = io.StringIO()
        full = wake.check_tool_entry(
            root,
            controller_label="wake-test",
            controller_claimed=True,
            mail_bearing=True,
            stream=full_stream,
        )
        assert full["live_waiters"] == full["target_waiters"] == wake.DEFAULT_LISTENER_SLOTS
        assert full_stream.getvalue() == ""

        # Kill down TO the low-water mark: a single missing slot is deliberately
        # silent now, so the graded hint needs a genuinely thin pool.
        for victim in processes[: wake.DEFAULT_LISTENER_SLOTS - wake.listener_low_water(wake.DEFAULT_LISTENER_SLOTS)]:
            victim.kill()
        processes[0].communicate(timeout=3)
        _wait_for_listener_count(
        root, label="wake-test", count=wake.listener_low_water(wake.DEFAULT_LISTENER_SLOTS)
    )
        reserve_stream = io.StringIO()
        reserve = wake.check_tool_entry(
            root,
            controller_label="wake-test",
            controller_claimed=True,
            mail_bearing=True,
            stream=reserve_stream,
        )
        assert reserve["covered"] is True  # old any-listener mutation would stop here.
        assert reserve["live_waiters"] == wake.listener_low_water(wake.DEFAULT_LISTENER_SLOTS)
        assert reserve["target_waiters"] == wake.DEFAULT_LISTENER_SLOTS
        hint = reserve_stream.getvalue()
        assert hint.startswith(
            f"listener pool n={wake.listener_low_water(wake.DEFAULT_LISTENER_SLOTS)}/"
            f"{wake.DEFAULT_LISTENER_SLOTS} — reserve down; re-arm: "
        )
        # One arm command per MISSING slot (at depth 2 that happened to equal
        # DEFAULT-1; state the real relation so any depth holds).
        assert hint.count("--report-pending") == (
            wake.DEFAULT_LISTENER_SLOTS - reserve["live_waiters"]
        )
    finally:
        lease_holder.close()
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3)


def test_cursor_advance_with_leftovers_pops_one_more_pool_member(
    isolated: tuple[Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    root, env = isolated
    authority = journal.open_or_create_journal(root)
    claimed = authority.claim_or_renew_lease(
        "wake-test", principal={"principal_id": "pool-leftovers"}
    )
    assert claimed.committed and claimed.value is not None
    lease_holder = _hold_claimed_lease(root, claimed.value)
    for dispatch_id in ("leftover-a", "leftover-b"):
        _post_notice(
            root,
            tmp_path,
            label="wake-test",
            dispatch_id=dispatch_id,
            text=dispatch_id,
        )
    # Deliberate three-listener arbitration against two leftover notices.
    arbitration_slots = _assert_arbitration_pool_legal(3)
    processes = [
        subprocess.Popen(
            _listener_command(
                root,
                tmp_path,
                label="wake-test",
                nonce=claimed.value.nonce,
                slots=arbitration_slots,
                timeout_s=_ARBITRATION_LISTENER_TIMEOUT_S,
                report_pending=False,
            ),
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _index in range(arbitration_slots)
    ]
    try:
        exited = _wait_for_exited_count(processes, 1)
        assert len(exited) == 1, _pool_exit_codes(processes)
        assert exited[0].returncode == 0, _pool_exit_codes(processes)
        _wait_for_listener_count(
            root, label="wake-test", count=arbitration_slots - 1
        )
        peek = authority.cursor_peek(
            "wake-test",
            nonce=claimed.value.nonce,
            limit=1000,
        )
        first_stream = sorted(peek.stream_snapshots)[0]
        first_position = max(
            int(item["stream_seq"])
            for item in peek.items
            if item["stream_id"] == first_stream
        )
        advanced = authority.advance_cursor(
            "wake-test",
            nonce=claimed.value.nonce,
            expected_cursor_version=peek.cursor_version,
            expected_stream_snapshots={first_stream: peek.stream_snapshots[first_stream]},
            advances={first_stream: first_position},
            actor="pool-leftover-test",
        )
        assert advanced.committed, advanced.reason
        assert len(authority.cursor_peek("wake-test", nonce=claimed.value.nonce).items) == 1

        exited = _wait_for_exited_count(processes, 2)
        assert len(exited) == 2, _pool_exit_codes(processes)
        assert all(process.returncode == 0 for process in exited), _pool_exit_codes(
            processes
        )
        assert sum(process.poll() is None for process in processes) == 1
        assert len(_wait_for_listener_count(root, label="wake-test", count=1)) == 1
    finally:
        lease_holder.close()
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3)


def test_listener_arm_past_target_is_not_refused_then_empty_pool_hint(
    isolated: tuple[Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    root, env = isolated
    authority = journal.open_or_create_journal(root)
    claimed = authority.claim_or_renew_lease(
        "wake-test", principal={"principal_id": "pool-exhaustion"}
    )
    assert claimed.committed and claimed.value is not None
    lease_holder = _hold_claimed_lease(root, claimed.value)
    command = _listener_command(
        root,
        tmp_path,
        label="wake-test",
        nonce=claimed.value.nonce,
        slots=3,
        timeout_s=20,
    )
    processes = [
        subprocess.Popen(
            command,
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _index in range(3)
    ]
    extra = None
    try:
        waiters = _wait_for_listener_count(root, label="wake-test", count=3)
        assert len(waiters) == 3
        extra = wake.register_listener_waiter(
            root,
            controller_label="wake-test",
            generation_key=claimed.value.nonce,
            slots=3,
        )
        assert extra.slot_index == 3
        assert all(process.poll() is None for process in processes)
        extra.close()
        extra = None

        for index in range(3):
            _post_notice(
                root,
                tmp_path,
                label="wake-test",
                dispatch_id=f"exhaust-{index}",
                text=f"ring {index}",
            )
            exited = _wait_for_exited_count(processes, index + 1)
            assert len(exited) == index + 1
            if index < 2:
                _advance_all(
                    authority,
                    label="wake-test",
                    nonce=claimed.value.nonce,
                )
        _wait_for_listener_count(root, label="wake-test", count=0)
        stream = io.StringIO()
        status_payload = wake.check_tool_entry(
            root,
            controller_label="wake-test",
            controller_claimed=True,
            mail_bearing=True,
            stream=stream,
        )
        assert status_payload["live_waiters"] == 0
        assert stream.getvalue().startswith("listener pool n=0; start: ")
    finally:
        lease_holder.close()
        if extra is not None:
            extra.close()
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3)


def test_listener_arm_has_no_slot_ceiling(
    isolated: tuple[Path, dict[str, str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, env = isolated
    monkeypatch.delenv("GOALFLIGHT_LISTENER_SLOTS", raising=False)
    env = dict(env)
    env.pop("GOALFLIGHT_LISTENER_SLOTS", None)
    authority = journal.open_or_create_journal(root)
    claimed = authority.claim_or_renew_lease(
        "wake-test", principal={"principal_id": "uncapped-arm"}
    )
    assert claimed.committed and claimed.value is not None
    nonce = claimed.value.nonce
    lease_holder = _hold_claimed_lease(root, claimed.value)
    holders: list[wake.WaiterRegistration] = []
    try:
        for expected_slot in range(40):
            holder = wake.register_listener_waiter(
                root,
                controller_label="wake-test",
                generation_key=nonce,
                slots=4,
            )
            holders.append(holder)
            assert holder.slot_index == expected_slot
        live = (
            wake.live_waiters(
                root, controller_label="wake-test", kinds={"listener"}
            )
            or []
        )
        assert len(live) == 40
        status_payload = wake.coverage_status(
            root, controller_label="wake-test", lease_nonce=nonce
        )
        assert status_payload["live_waiters"] == 40
        assert status_payload["target_waiters"] == wake.DEFAULT_LISTENER_SLOTS
        forty_first = wake.register_listener_waiter(
            root,
            controller_label="wake-test",
            generation_key=nonce,
            slots=4,
        )
        holders.append(forty_first)
        assert forty_first.slot_index == 40
        contender = subprocess.run(
            _listener_command(
                root,
                tmp_path,
                label="wake-test",
                nonce=nonce,
                slots=4,
                timeout_s=0.2,
                report_pending=False,
            ),
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert contender.returncode == 1, (contender.stdout, contender.stderr)
        assert "hold live doorbells" not in contender.stderr
        assert "full" not in contender.stderr.lower()
        help_text = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "goalflight_messages.py"),
                "listen",
                "--help",
            ],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert help_text.returncode == 0, help_text.stderr
        # This test is about the CEILING, so pin the claim that is actually
        # load-bearing here: arming is never refused for pool depth. The help
        # text used to say "exit 3 means mail pending only" and this asserted
        # it, but that claim is false - cmd_listen also returns 3 for a held
        # watchdog slot, for both orphaned cases, and for a stale lease. Pinning
        # a false sentence made the docs harder to correct than to leave wrong.
        assert "never a full-pool refusal" in help_text.stdout
        assert "not a ceiling" in help_text.stdout
    finally:
        lease_holder.close()
        for holder in holders:
            holder.close()


def test_create_path_lock_failure_is_not_treated_as_slot_taken(
    isolated: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EAGAIN on a unique new inode is fail-closed, not 'try slot N+1'."""
    root, _env = isolated
    monkeypatch.delenv("GOALFLIGHT_LISTENER_SLOTS", raising=False)
    authority = journal.open_or_create_journal(root)
    claimed = authority.claim_or_renew_lease(
        "wake-test", principal={"principal_id": "create-lock-fail"}
    )
    assert claimed.committed and claimed.value is not None
    attempts = {"n": 0}

    def boom(_fd: int) -> None:
        attempts["n"] += 1
        if attempts["n"] > 3:
            raise AssertionError(
                "create-path BlockingIOError was treated as a taken slot"
            )
        raise BlockingIOError(errno.EAGAIN, "locked")

    monkeypatch.setattr(wake, "_lock_nonblocking", boom)
    with pytest.raises(RuntimeError, match="newly created"):
        wake.register_listener_waiter(
            root,
            controller_label="wake-test",
            generation_key=claimed.value.nonce,
            slots=4,
        )
    assert attempts["n"] == 1


def test_self_post_does_not_ring_pool_foreign_post_does(
    isolated: tuple[Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    root, env = isolated
    authority = journal.open_or_create_journal(root)
    claimed = authority.claim_or_renew_lease(
        "wake-test", principal={"principal_id": "self-wake-pool"}
    )
    assert claimed.committed and claimed.value is not None
    lease_holder = _hold_claimed_lease(root, claimed.value)
    # Two-listener self-vs-foreign arbitration.
    arbitration_slots = _assert_arbitration_pool_legal(2)
    processes = [
        subprocess.Popen(
            _listener_command(
                root,
                tmp_path,
                label="wake-test",
                nonce=claimed.value.nonce,
                slots=arbitration_slots,
                timeout_s=_ARBITRATION_LISTENER_TIMEOUT_S,
            ),
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _index in range(arbitration_slots)
    ]
    try:
        _wait_for_listener_count(root, label="wake-test", count=arbitration_slots)
        assert all(process.poll() is None for process in processes)
        self_post = _post_notice(
            root,
            tmp_path,
            label="wake-test",
            dispatch_id="self-authored",
            text="my own receipt",
            source={
                "node": "local",
                "adapter": "pytest",
                "transport": "controller",
                "controller_label": "wake-test",
            },
            author_capability=claimed.value.nonce,
        )
        assert messages.envelope_authored_by_controller(
            self_post,
            controller_label="wake-test",
            lease_nonce=claimed.value.nonce,
        )
        assert all(process.poll() is None for process in processes)

        spoofed_post = _post_notice(
            root,
            tmp_path,
            label="wake-test",
            dispatch_id="label-spoofed-foreign",
            text="wake despite my claimed label",
            source={
                "node": "foreign",
                "adapter": "pytest",
                "transport": "controller",
                "controller_label": "wake-test",
            },
        )
        assert "author_digest" not in spoofed_post
        assert not messages.envelope_authored_by_controller(
            spoofed_post,
            controller_label="wake-test",
            lease_nonce=claimed.value.nonce,
        )
        # A labels-instead-of-digests mutation returns true and makes the ring
        # assertion below time out, so the spoof path kills that exact mutant.
        assert spoofed_post["source"]["controller_label"] == "wake-test"
        exited = _wait_for_exited_count(processes, 1)
        assert len(exited) == 1
        assert exited[0].returncode == 0
        assert len(authority.cursor_peek("wake-test", nonce=claimed.value.nonce).items) == 2
        _advance_all(authority, label="wake-test", nonce=claimed.value.nonce)
        assert not authority.cursor_peek("wake-test", nonce=claimed.value.nonce).items
    finally:
        lease_holder.close()
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3)


def test_detached_listener_refuses_ppid_one_with_distinct_code_and_one_line(
    isolated: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _env = isolated
    authority = journal.open_or_create_journal(root)
    claimed = authority.claim_or_renew_lease(
        "wake-test", principal={"principal_id": "detached-code-test"}
    )
    assert claimed.committed and claimed.value is not None
    lease_holder = _hold_claimed_lease(root, claimed.value)
    try:
        monkeypatch.setenv("GOALFLIGHT_LISTENER_STARTUP_GRACE_S", "0.05")
        monkeypatch.setattr(messages.os, "getppid", lambda: 1)
        args = SimpleNamespace(
            project_root=str(root),
            controller_label="wake-test",
            lease_nonce=claimed.value.nonce,
            poll_secs=0.01,
            listener_slots=1,
            timeout_s=2,
            json=False,
            report_pending=False,
        )

        prior_term = signal.getsignal(signal.SIGTERM)
        code = messages.cmd_listen(args)
        captured = capsys.readouterr()
        assert signal.getsignal(signal.SIGTERM) == prior_term

        assert code == messages.DETACHED_LISTENER_EXIT_CODE
        assert code not in {0, 1, 2, 3, 5}
        # 144 is POSIX 128+SIGURG on macOS, not the detached refusal.
        assert code != 144
        assert messages.DETACHED_LISTENER_EXIT_CODE != 128 + int(signal.SIGURG)
        assert captured.out == ""
        lines = captured.err.splitlines()
        assert len(lines) == 1
        assert lines[0].startswith(
            f"DETACHED LISTENER: my exit wakes nobody; kill me (pid {os.getpid()}) "
            "and re-arm as a tracked background task: "
        )
        assert "--report-pending" in lines[0]
    finally:
        lease_holder.close()


def test_shell_detached_listener_is_reaped_after_startup_grace_real_process(
    isolated: tuple[Path, dict[str, str]],
    tmp_path: Path,
) -> None:
    root, env = isolated
    authority = journal.open_or_create_journal(root)
    claimed = authority.claim_or_renew_lease(
        "wake-test", principal={"principal_id": "detached-regression"}
    )
    assert claimed.committed and claimed.value is not None
    lease_holder = _hold_claimed_lease(root, claimed.value)
    env = dict(env)
    env["GOALFLIGHT_LISTENER_STARTUP_GRACE_S"] = "0.2"
    # The re-arm hint now names the ADVERTISED install rather than the running
    # copy, so that a listener started from a development checkout does not tell
    # its reader to re-arm that checkout. Pin the advertised root to the code
    # under test: without this the expectation below silently depends on whether
    # the host happens to have ~/.goal-flight/skill installed -- it would pass on
    # a bare box (no pin, fallback to the running copy) and fail on a real one.
    env["GOALFLIGHT_ROOT"] = str(ROOT)
    command = _listener_command(
        root,
        tmp_path,
        label="wake-test",
        nonce=claimed.value.nonce,
        slots=1,
        timeout_s=10,
    )
    stdout_path = tmp_path / "detached-listener.stdout"
    stderr_path = tmp_path / "detached-listener.stderr"
    pid_path = tmp_path / "detached-listener.pid"
    launcher_code = (
        "import subprocess; from pathlib import Path; "
        f"out=open({str(stdout_path)!r},'w'); err=open({str(stderr_path)!r},'w'); "
        f"p=subprocess.Popen({command!r},cwd={str(root)!r},stdout=out,stderr=err,"
        "text=True,start_new_session=True); "
        f"Path({str(pid_path)!r}).write_text(str(p.pid)); out.close(); err.close()"
    )
    launcher = subprocess.run(
        [sys.executable, "-c", launcher_code],
        cwd=root,
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
        assert lines[0] == (
            "DETACHED LISTENER: my exit wakes nobody; kill me "
            f"(pid {listener_pid}) and re-arm as a tracked background task: "
            f"{shlex.join([*command, '--report-pending'])}"
        )
        assert stdout_path.read_text(encoding="utf-8") == ""
        _wait_for_listener_count(root, label="wake-test", count=0)
    finally:
        lease_holder.close()
        with contextlib.suppress(ProcessLookupError):
            os.kill(listener_pid, signal.SIGKILL)


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
            "live_waiters": None,
            "target_waiters": wake.DEFAULT_LISTENER_SLOTS,
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
    assert offline_stream.getvalue().startswith("listener pool n=0; start: ")
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


def test_claim_expires_dead_recipients_but_keeps_live_and_unknown_holders(
    isolated: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _env = isolated
    authority = journal.open_or_create_journal(root)
    dead = authority.claim_or_renew_lease(
        "dead", principal={"principal_id": "dead-controller"}
    )
    assert dead.committed and dead.value is not None
    dead_holder = wake.register_lease_holder(
        root,
        controller_label="dead",
        lease_nonce=dead.value.nonce,
    )
    live = authority.claim_or_renew_lease(
        "live", principal={"principal_id": "live-controller"}
    )
    assert live.committed and live.value is not None
    live_holder = wake.register_lease_holder(
        root,
        controller_label="live",
        lease_nonce=live.value.nonce,
    )
    unknown = authority.claim_or_renew_lease(
        "unknown", principal={"principal_id": "unknown-controller"}
    )
    assert unknown.committed and unknown.value is not None
    dead_holder.close()

    real_liveness = journal.goalflight_wake.lease_holder_alive
    observations: list[tuple[str, bool]] = []

    def observe_liveness(
        project_root: Path | str,
        *,
        controller_label: str,
        lease_nonce: str,
        prune_dead: bool = True,
    ) -> bool | None:
        observations.append((controller_label, prune_dead))
        return real_liveness(
            project_root,
            controller_label=controller_label,
            lease_nonce=lease_nonce,
            prune_dead=prune_dead,
        )

    monkeypatch.setattr(
        journal.goalflight_wake,
        "lease_holder_alive",
        observe_liveness,
    )
    try:
        swept = authority.claim_or_renew_lease(
            "sweeper", principal={"principal_id": "active-controller"}
        )
        assert swept.committed and swept.value is not None

        active_recipients = messages._active_controller_labels(authority)
        assert "dead" not in active_recipients
        assert {"live", "unknown", "sweeper"}.issubset(active_recipients)
        assert observations
        assert all(prune_dead is False for _label, prune_dead in observations)

        dead_row = next(
            row
            for row in authority.lease_records(include_ended=True)
            if row["label"] == "dead"
        )
        assert dead_row["state"] == "EXPIRED"
        assert dead_row["ended_reason"] == "holder-dead"
        assert authority.active_lease("live") == live.value
        assert authority.active_lease("unknown") == unknown.value
    finally:
        live_holder.close()


def test_expire_stale_leases_is_idempotent(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    root, _env = isolated
    authority = journal.open_or_create_journal(root)
    claimed = authority.claim_or_renew_lease(
        "dead", principal={"principal_id": "dead-controller"}
    )
    assert claimed.committed and claimed.value is not None
    holder = wake.register_lease_holder(
        root,
        controller_label="dead",
        lease_nonce=claimed.value.nonce,
    )
    holder.close()

    first = authority.expire_stale_leases()
    second = authority.expire_stale_leases()

    assert first.committed and first.value is not None
    assert [row["label"] for row in first.value] == ["dead"]
    assert second.committed and second.value == []
    assert authority.active_lease("dead") is None
    ended = [
        row
        for row in authority.lease_records(include_ended=True)
        if row["label"] == "dead" and row["state"] == "EXPIRED"
    ]
    assert len(ended) == 1


def test_stale_expiry_snapshot_cannot_expire_a_concurrent_renewal(
    isolated: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _env = isolated
    authority = journal.open_or_create_journal(root)
    incumbent = authority.claim_or_renew_lease(
        "owner", principal={"principal_id": "dead-controller"}
    )
    assert incumbent.committed and incumbent.value is not None
    holder = wake.register_lease_holder(
        root,
        controller_label="owner",
        lease_nonce=incumbent.value.nonce,
    )
    holder.close()

    observed_dead = threading.Event()
    release_expiry = threading.Event()
    real_liveness = journal.goalflight_wake.lease_holder_alive

    def delayed_liveness(
        project_root: Path | str,
        *,
        controller_label: str,
        lease_nonce: str,
        prune_dead: bool = True,
    ) -> bool | None:
        if threading.current_thread().name != "stale-expiry":
            return None
        alive = real_liveness(
            project_root,
            controller_label=controller_label,
            lease_nonce=lease_nonce,
            prune_dead=prune_dead,
        )
        observed_dead.set()
        assert release_expiry.wait(timeout=3)
        return alive

    monkeypatch.setattr(
        journal.goalflight_wake,
        "lease_holder_alive",
        delayed_liveness,
    )
    expiry_result: dict[str, object] = {}

    def expire() -> None:
        expiry_result["result"] = journal.Journal(root).expire_stale_leases()

    thread = threading.Thread(target=expire, name="stale-expiry")
    thread.start()
    try:
        assert observed_dead.wait(timeout=3)
        renewal = authority.claim_or_renew_lease(
            "owner",
            principal={"principal_id": "dead-controller"},
        )
        assert renewal.committed and renewal.value is not None
    finally:
        release_expiry.set()
        thread.join(timeout=3)

    assert not thread.is_alive()
    result = expiry_result["result"]
    assert isinstance(result, journal.WriteResult)
    assert result.committed and result.value == []
    assert renewal.value.generation == incumbent.value.generation
    assert renewal.value.renewed_at != incumbent.value.renewed_at
    assert authority.active_lease("owner") == renewal.value


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
    lease_holder = _hold_claimed_lease(root, claimed.value)
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
        lease_holder.close()
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
    lease_holder = _hold_claimed_lease(root, claimed.value)
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
        "60",
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
        _wait_for_listener(root, "wake-test", healthy)
        coverage = journal.Journal(root, retry_budget_s=10.0).active_coverage("wake-test")
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
        current = journal.Journal(root, retry_budget_s=10.0).active_coverage("wake-test")
        assert current is not None and current["coverage_id"] == coverage_id
        assert healthy.poll() is None
        assert wake.coverage_status(root, controller_label="wake-test")["covered"] is True
    finally:
        lease_holder.close()
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
    lease_holder = _hold_claimed_lease(root, claimed.value)
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
        # A broken wake-ledger path cannot confirm holder liveness, so listen
        # refuses before waiter registration rather than displacing coverage.
        assert "journal-unavailable" in replacement.stderr
        assert "did-not-arm" not in replacement.stderr
        current = authority.active_coverage("wake-test")
        assert current is not None
        assert current["coverage_id"] == original_coverage["coverage_id"]
        assert healthy.poll() is None
        assert wake.coverage_status(root, controller_label="wake-test")["covered"] is True
    finally:
        lease_holder.close()
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
        assert "listener pool n=0; start: " not in result.stderr, (
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
                if line.startswith("listener pool n=0; start: ")
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
    # The advertised command is pasteable (`python3`); run it with the
    # pytest interpreter so PATH python is not a hidden precondition.
    if argv and argv[0] == "python3":
        argv[0] = sys.executable
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
    assert stream.getvalue().startswith("listener pool n=0; start: ")


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


def test_full_listener_pool_coverage_skips_notice_and_poll(
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
    with contextlib.ExitStack() as pool:
        for _slot in range(wake.DEFAULT_LISTENER_SLOTS):
            pool.enter_context(
                wake.register_listener_waiter(
                    root,
                    controller_label="wake-test",
                    generation_key=claimed.value.nonce,
                    slots=wake.DEFAULT_LISTENER_SLOTS,
                )
            )
        stream = io.StringIO()
        result = messages.emit_wake_entry_notice(project_root=root, stream=stream)
    lease_holder.close()

    assert result["covered"] is True
    assert result["reason"] == "held-flock"
    assert result["live_waiters"] == result["target_waiters"] == wake.DEFAULT_LISTENER_SLOTS
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
            carrier = journal._synthetic_journal_carrier(
                journal._JOURNAL_RESUME_CARRIER_KIND, item_id
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
                (str(root.resolve()), item_id, seq, carrier, now, now),
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
        "SELECT carrier_path, withdrawn_at FROM delivery_events "
        "WHERE event_type = 'controller_attention'"
    )
    resume_prefix = (
        f"{journal._JOURNAL_CARRIER_PREFIX}{journal._JOURNAL_RESUME_CARRIER_KIND}:"
    )
    assert all(str(row["carrier_path"]).startswith(resume_prefix) for row in withdrawn)
    assert sum(row["withdrawn_at"] is not None for row in withdrawn) == 2


def test_lease_horizon_outlives_wait_heartbeat_and_hourly_watchdog() -> None:
    assert journal.DEFAULT_LEASE_HORIZON_S >= 2 * 60 * 60
    assert journal.DEFAULT_LEASE_HORIZON_S > status._WAIT_HEARTBEAT_S

def test_hint_stays_quiet_until_the_pool_runs_low() -> None:
    """A missing slot is not news; a thin pool is.

    Operator ruling 2026-08-16: hints must not nag at 3/4. Silence holds
    while depth is above the low-water mark, and only then does the hint
    speak — with the loud offline wording reserved for an empty pool.
    """
    target = wake.DEFAULT_LISTENER_SLOTS
    low = wake.listener_low_water(target)
    assert 1 <= low < target, "low water must leave healthy depth silent"

    for healthy in range(low + 1, target + 1):
        assert wake.listener_reserve_hint(healthy, target, "CMD") == "", (
            f"pool n={healthy}/{target} should be silent"
        )
    for thin in range(1, low + 1):
        hint = wake.listener_reserve_hint(thin, target, "CMD")
        assert hint.startswith(f"listener pool n={thin}/{target}")
        assert hint.count("CMD") == target - thin
    assert wake.listener_reserve_hint(0, target, "CMD").startswith("listener pool n=0;")


def test_low_water_override_is_honored_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOALFLIGHT_LISTENER_LOW_WATER", "3")
    assert wake.listener_low_water(4) == 3
    assert wake.listener_reserve_hint(3, 4, "CMD") != ""
    monkeypatch.setenv("GOALFLIGHT_LISTENER_LOW_WATER", "99")
    assert wake.listener_low_water(4) == 4, "override cannot exceed the target"
    monkeypatch.setenv("GOALFLIGHT_LISTENER_LOW_WATER", "nonsense")
    with pytest.raises(ValueError):
        wake.listener_low_water(4)
