#!/usr/bin/env python3
"""Dispatch launch ownership is persist-or-refuse unless explicitly forced."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("dispatch registration gate launches POSIX workers")

import argparse
import asyncio
import json
import multiprocessing
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
DISPATCH = SCRIPTS / "goalflight_dispatch.py"
sys.path.insert(0, str(SCRIPTS))

import goalflight_capacity as capacity  # noqa: E402
import goalflight_acp_run as acp  # noqa: E402
import goalflight_dispatch as dispatch  # noqa: E402
import goalflight_journal as journal  # noqa: E402
import goalflight_ledger as ledger  # noqa: E402
import goalflight_task as task  # noqa: E402
import goalflight_wake as wake  # noqa: E402


class _FutureJournalUnavailable(journal.JournalUnavailable):
    pass


def _hold_sibling_controller(
    project: Path,
    ready,
    stop,
) -> None:
    authority = journal.open_or_create_journal(project)
    principal = ledger.process_identity(os.getpid())
    if principal is None:
        ready.put({"error": "missing_process_identity"})
        return
    claimed = authority.claim_or_renew_lease(
        "sibling-controller",
        principal=principal,
    )
    if not claimed.committed or claimed.value is None:
        ready.put({"error": "lease_claim_failed"})
        return
    holder = wake.register_lease_holder(
        project,
        controller_label="sibling-controller",
        lease_nonce=claimed.value.nonce,
    )
    ready.put(
        {
            "pid": os.getpid(),
            "nonce": claimed.value.nonce,
        }
    )
    try:
        stop.wait(20)
    finally:
        holder.close()


def _isolated_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[dict[str, str], Path, Path]:
    project = tmp_path / "project"
    project.mkdir()
    advertised = tmp_path / "advertised install"
    values = {
        "GOALFLIGHT_STATE_DIR": tmp_path / "state",
        "GOALFLIGHT_DISPATCH_DIR": tmp_path / "dispatch",
        "GOALFLIGHT_TASK_STORE_DIR": tmp_path / "task-store",
        "GOALFLIGHT_JOURNAL_DIR": tmp_path / "journal",
        "GOALFLIGHT_MESSAGES_DIR": tmp_path / "messages",
        "GOALFLIGHT_WAKE_LEDGER_DIR": tmp_path / "wake-ledger",
        "GOAL_FLIGHT_PIDFILE_DIR": tmp_path / "pidfiles",
        "GOALFLIGHT_PIDFILE_DIR": tmp_path / "pidfiles",
        "GOALFLIGHT_ROOT": advertised,
    }
    for key, value in values.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", "/dev/null")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_WAIT_S", "0")
    for key in (
        "GOALFLIGHT_CONTROLLER_LABEL",
        "GOALFLIGHT_CONTROLLER_PID",
        "GOALFLIGHT_CONTROLLER_SESSION_ID",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE",
    ):
        monkeypatch.delenv(key, raising=False)
    return os.environ.copy(), project, advertised


def _run_dispatch(
    env: dict[str, str],
    project: Path,
    dispatch_id: str,
    sentinel: Path,
    *,
    forced: bool = False,
    controller_label: str | None = None,
    controller_pid: int | None = None,
    controller_session_id: str | None = None,
) -> subprocess.CompletedProcess[str]:
    worker = (
        "from pathlib import Path; import os; "
        f"Path({str(sentinel)!r}).write_text(str(os.getpid()), encoding='utf-8'); "
        f"print('!COMPLETE: {dispatch_id} — worker ran', flush=True)"
    )
    argv = [
        sys.executable,
        str(DISPATCH),
        "--agent",
        "codex",
        "--foreground",
        "--dispatch-id",
        dispatch_id,
        "--cwd",
        str(project),
        "--poll-secs",
        "0.1",
        "--max-idle-secs",
        "5",
        "--capacity-wait-s",
        "0",
        "--tail",
        str(project.parent / f"{dispatch_id}.tail"),
    ]
    if forced:
        argv.append("--unregistered-forced")
    for flag, value in (
        ("--controller-label", controller_label),
        ("--controller-pid", controller_pid),
        ("--controller-session-id", controller_session_id),
    ):
        if value is not None:
            argv.extend([flag, str(value)])
    argv.extend(["--", sys.executable, "-c", worker])
    return subprocess.run(
        argv,
        cwd=project,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )


def _capacity_leases(dispatch_id: str) -> list[dict]:
    return [
        item
        for item in capacity.load_state().get("leases", {}).values()
        if item.get("dispatch_id") == dispatch_id
    ]


def _direct_acp_cfg(project: Path, tmp_path: Path, dispatch_id: str):
    return acp.normalized_acp_dispatch_cfg(
        argparse.Namespace(
            agent="codex-acp",
            model=None,
            install_slot=None,
            cwd=str(project),
            worktree="off",
            session_id=None,
            dispatch_id=dispatch_id,
            priority="normal",
            capacity_wait_s=0.0,
            prompt_id=None,
            prompt=None,
            prompt_text="COMPLETE: direct ACP gate probe",
            prompt_b64=None,
            original_prompt_file=None,
            mode="one-shot",
            idle_timeout=5.0,
            status_json=str(tmp_path / f"{dispatch_id}.status.json"),
            steer_file=str(tmp_path / f"{dispatch_id}.steer.jsonl"),
            context_mode="disabled",
            os_sandbox=acp.OS_SANDBOX_OFF,
            permission_mode="auto",
            permission_dir=None,
            permission_inline_timeout_s=None,
            permission_user_timeout_s=None,
            permission_allow_tool_title_pattern=[],
            read_only=False,
            interactive=False,
            heartbeat_interval=0.05,
            wedge_samples=1,
            max_tool_s=5.0,
            max_consecutive_tool_errors=5,
            max_acp_events=100,
            max_quiet_s=2.0,
            progress_stall_s=2.0,
            stall_kill=False,
            liveness_profile="local_compute",
            remote_turn_silence_s=None,
            remote_turn_cancel_grace_s=1.0,
            cpu_epsilon=0.1,
            controller_label=None,
            controller_pid=None,
            controller_session_id=None,
            unregistered_forced=False,
            json=False,
        )
    )


def _stub_direct_acp_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acp, "agent_command", lambda *_args, **_kwargs: ("fake", []))
    monkeypatch.setattr(
        acp,
        "_codex_workspace_write_acp_args",
        lambda _agent, args, **_kwargs: args,
    )
    monkeypatch.setattr(acp, "validate_acp_dispatch_readiness", lambda *_args: None)
    monkeypatch.setattr(acp, "validate_os_sandbox_request", lambda *_args: None)
    monkeypatch.setattr(acp, "preflight_os_sandbox", lambda *_args: None)


def _register_controller(
    project: Path,
    env: dict[str, str],
    *,
    label: str = "registered-test",
    export_identity: bool = True,
) -> tuple[journal.Journal, object, str]:
    authority = journal.open_or_create_journal(project)
    principal = ledger.process_identity(os.getpid())
    assert principal is not None
    claimed = authority.claim_or_renew_lease(label, principal=principal)
    assert claimed.committed and claimed.value is not None
    holder = wake.register_lease_holder(
        project,
        controller_label=label,
        lease_nonce=claimed.value.nonce,
    )
    if export_identity:
        env.update(
            {
                "GOALFLIGHT_CONTROLLER_LABEL": label,
                "GOALFLIGHT_CONTROLLER_PID": str(os.getpid()),
                "GOALFLIGHT_CONTROLLER_LEASE_NONCE": claimed.value.nonce,
            }
        )
    return authority, holder, claimed.value.nonce


def _attempt_owner(authority: journal.Journal, dispatch_id: str) -> dict:
    return authority.read_all(
        """SELECT owner_controller_label, owner_session_digest
           FROM dispatch_attempts WHERE dispatch_id = ?""",
        (dispatch_id,),
    )[0]


def _unowned_registration_args(*, forced: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        controller_session_id=None,
        _controller_beacon_pid=None,
        controller_label=None,
        _requested_controller_label=None,
        _requested_controller_pid=None,
        unregistered_forced=forced,
    )


def test_controller_registry_lookup_does_not_take_journal_write_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env, project, _advertised = _isolated_env(monkeypatch, tmp_path)
    authority, holder, nonce = _register_controller(
        project,
        env,
        export_identity=False,
    )
    outcome: dict[str, object] = {}

    def read_registry() -> None:
        try:
            outcome["lookup"] = dispatch._kernel_live_controller_sessions(project)
        except BaseException as exc:  # pragma: no cover - asserted below
            outcome["error"] = exc

    thread = threading.Thread(target=read_registry)
    try:
        with task.FileLock(journal.journal_write_lock_path(authority.path)):
            thread.start()
            thread.join(timeout=0.5)
            completed_while_locked = not thread.is_alive()
    finally:
        holder.close()
    thread.join(timeout=5)

    assert completed_while_locked, "controller registry lookup took the write lock"
    assert not thread.is_alive()
    assert "error" not in outcome
    lookup = outcome["lookup"]
    assert isinstance(lookup, dispatch._ControllerSessionLookup)
    assert lookup.unreadable_reason is None
    assert lookup.sessions is not None
    assert [session["id"] for session in lookup.sessions] == [nonce]


def test_busy_controller_registry_refuses_unforced_with_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _env, project, _advertised = _isolated_env(monkeypatch, tmp_path)
    authority = journal.open_or_create_journal(project)

    with sqlite3.connect(
        authority.path,
        timeout=0,
        isolation_level=None,
    ) as blocker:
        assert blocker.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
        blocker.execute("BEGIN EXCLUSIVE")
        with pytest.raises(dispatch.DispatchUsageError) as error:
            dispatch._prepare_attempt_controller_registration(
                _unowned_registration_args(),
                project,
            )

    message = str(error.value)
    assert "controller registry could not be read" in message
    assert "journal busy" in message
    assert "Retry the dispatch" in message
    assert "controller is not registered" not in message
    assert "--unregistered-forced" not in message
    assert "ownership could not be determined" not in message


def test_busy_controller_registry_forced_warns_that_ownership_is_undetermined(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _env, project, _advertised = _isolated_env(monkeypatch, tmp_path)
    authority = journal.open_or_create_journal(project)

    with sqlite3.connect(
        authority.path,
        timeout=0,
        isolation_level=None,
    ) as blocker:
        assert blocker.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
        blocker.execute("BEGIN EXCLUSIVE")
        warning = dispatch._prepare_attempt_controller_registration(
            _unowned_registration_args(forced=True),
            project,
        )

    assert warning is not None
    assert "controller registry could not be read" in warning
    assert "ownership could not be determined" in warning
    assert "journal busy" in warning
    assert "--unregistered-forced accepted" in warning
    assert "controller is not registered" not in warning
    assert "Retry the dispatch" not in warning


def test_unreadable_live_lease_makes_whole_registry_lookup_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env, project, _advertised = _isolated_env(monkeypatch, tmp_path)
    _authority, holder, _nonce = _register_controller(
        project,
        env,
        export_identity=False,
    )
    monkeypatch.setattr(
        dispatch.goalflight_session_status,
        "probe_live_session",
        lambda *_args, **_kwargs: ("unreadable", None),
    )
    monkeypatch.setattr(
        dispatch.goalflight_session_status,
        "live_session",
        lambda *_args, **_kwargs: pytest.fail("legacy collapsing wrapper was used"),
    )
    try:
        lookup = dispatch._kernel_live_controller_sessions(project)
        with pytest.raises(dispatch.DispatchUsageError) as error:
            dispatch._prepare_attempt_controller_registration(
                _unowned_registration_args(),
                project,
            )
        forced = dispatch._prepare_attempt_controller_registration(
            _unowned_registration_args(forced=True),
            project,
        )
    finally:
        holder.close()

    assert lookup.sessions is None
    assert "registered-test" in str(lookup.unreadable_reason)
    assert "unreadable" in str(lookup.unreadable_reason)
    assert "controller registry could not be read" in str(error.value)
    assert "Retry the dispatch" in str(error.value)
    assert "--unregistered-forced" not in str(error.value)
    assert forced is not None
    assert "ownership could not be determined" in forced
    assert "--unregistered-forced accepted" in forced
    assert "controller is not registered" not in forced


def test_resolve_owner_none_falls_through_to_three_state_lookup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _env, project, _advertised = _isolated_env(monkeypatch, tmp_path)
    authority = journal.open_or_create_journal(project)
    monkeypatch.setattr(
        dispatch.goalflight_session_status,
        "live_session",
        lambda *_args, **_kwargs: None,
    )
    args = argparse.Namespace(
        controller_session_id="stamped-nonce",
        _controller_beacon_pid=os.getpid(),
        controller_label="stamped",
        _requested_controller_label="stamped",
        _requested_controller_pid=os.getpid(),
        unregistered_forced=False,
    )

    with sqlite3.connect(
        authority.path,
        timeout=0,
        isolation_level=None,
    ) as blocker:
        assert blocker.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
        blocker.execute("BEGIN EXCLUSIVE")
        with pytest.raises(dispatch.DispatchUsageError) as error:
            dispatch._prepare_attempt_controller_registration(args, project)

    message = str(error.value)
    assert "controller registry could not be read" in message
    assert "journal busy" in message
    assert "controller is not registered" not in message


@pytest.mark.parametrize(
    ("failure_type", "expected_reason"),
    [
        (journal.JournalIOError, "journal I/O error"),
        (_FutureJournalUnavailable, "journal unavailable (_FutureJournalUnavailable)"),
    ],
)
def test_all_non_disappeared_journal_unavailability_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_type: type[journal.JournalUnavailable],
    expected_reason: str,
) -> None:
    _env, project, _advertised = _isolated_env(monkeypatch, tmp_path)
    journal.open_or_create_journal(project)

    def unavailable(_cls, _project_root):
        raise failure_type("injected registry read failure")

    monkeypatch.setattr(journal.Journal, "open_reader", classmethod(unavailable))

    lookup = dispatch._kernel_live_controller_sessions(project)

    assert lookup.sessions is None
    assert expected_reason in str(lookup.unreadable_reason)


def test_absent_controller_registry_remains_definitely_unregistered(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _env, project, _advertised = _isolated_env(monkeypatch, tmp_path)
    write_ctor_calls: list[object] = []
    reader_calls: list[object] = []
    real_init = journal.Journal.__init__
    real_open_reader = journal.Journal.open_reader

    def tracking_init(self, *args, **kwargs):
        write_ctor_calls.append(True)
        return real_init(self, *args, **kwargs)

    def tracking_open_reader(cls, project_root, **kwargs):
        reader_calls.append(project_root)
        return real_open_reader(project_root, **kwargs)

    monkeypatch.setattr(journal.Journal, "__init__", tracking_init)
    monkeypatch.setattr(
        journal.Journal,
        "open_reader",
        classmethod(tracking_open_reader),
    )

    lookup = dispatch._kernel_live_controller_sessions(project)

    assert reader_calls, "absent journal must be observed via Journal.open_reader"
    assert write_ctor_calls == [], (
        "absent journal must not take the journal write constructor"
    )
    assert isinstance(lookup, dispatch._ControllerSessionLookup)
    assert lookup.sessions == []
    assert lookup.unreadable_reason is None

    warning = dispatch._controller_registration_warning(
        _unowned_registration_args(),
        project,
        lookup=lookup,
    )
    assert "controller is not registered" in warning
    assert "controller registry could not be read" not in warning

    with pytest.raises(dispatch.DispatchUsageError) as error:
        dispatch._prepare_attempt_controller_registration(
            _unowned_registration_args(),
            project,
        )
    assert "controller is not registered" in str(error.value)
    assert "could not be read" not in str(error.value)

    forced = dispatch._prepare_attempt_controller_registration(
        _unowned_registration_args(forced=True),
        project,
    )
    assert forced is not None
    assert "controller is not registered" in forced
    assert "ownership could not be determined" not in forced


def test_registered_controller_launch_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env, project, _advertised = _isolated_env(monkeypatch, tmp_path)
    authority, holder, nonce = _register_controller(project, env)
    dispatch_id = "registered-controller-launch"
    sentinel = tmp_path / "registered-worker.pid"
    try:
        completed = _run_dispatch(env, project, dispatch_id, sentinel)
    finally:
        holder.close()

    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert completed.stderr.count("[goal-flight] dispatched") == 1
    assert "controller is not registered" not in completed.stderr
    assert "--unregistered-forced" not in completed.stderr
    assert "controller is not registered" not in completed.stdout
    assert "DISPATCH-START " in completed.stdout
    assert "DISPATCH-END " in completed.stdout
    assert sentinel.is_file()
    record = json.loads(ledger.record_path(dispatch_id, create=False).read_text())
    assert record["controller_session_id"] == nonce
    assert record["controller_pid"] == os.getpid()
    assert record["controller_label"] == "registered-test"
    owner = _attempt_owner(authority, dispatch_id)
    assert owner["owner_controller_label"] == "registered-test"
    assert owner["owner_session_digest"] == wake.controller_session_digest(nonce)


def test_registered_controller_without_ownership_inputs_self_resolves(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env, project, _advertised = _isolated_env(monkeypatch, tmp_path)
    authority, holder, nonce = _register_controller(
        project,
        env,
        export_identity=False,
    )
    dispatch_id = "registered-controller-flagless"
    sentinel = tmp_path / "flagless-worker.pid"
    try:
        completed = _run_dispatch(env, project, dispatch_id, sentinel)
    finally:
        holder.close()

    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert sentinel.is_file()
    owner = _attempt_owner(authority, dispatch_id)
    assert owner["owner_controller_label"] == "registered-test"
    assert owner["owner_session_digest"] == wake.controller_session_digest(nonce)


def test_dead_sql_active_lease_does_not_make_live_owner_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env, project, _advertised = _isolated_env(monkeypatch, tmp_path)
    authority = journal.open_or_create_journal(project)
    dead_principal = ledger.process_identity(os.getpid())
    assert dead_principal is not None
    dead_principal = {
        **dead_principal,
        "pid": 2_147_000_000,
        "start_token": "dead-controller",
    }
    dead = authority.claim_or_renew_lease(
        "dead-sql-active",
        principal=dead_principal,
    )
    assert dead.committed and dead.value is not None
    authority, holder, _nonce = _register_controller(
        project,
        env,
        label="only-kernel-live",
        export_identity=False,
    )
    dispatch_id = "dead-active-row-ignored"
    sentinel = tmp_path / "dead-active-row-worker.pid"
    forced_id = "dead-active-row-forced"
    forced_sentinel = tmp_path / "dead-active-row-forced-worker.pid"
    try:
        lookup = dispatch._kernel_live_controller_sessions(project)
        completed = _run_dispatch(env, project, dispatch_id, sentinel)
        forced = _run_dispatch(
            env,
            project,
            forced_id,
            forced_sentinel,
            forced=True,
        )
    finally:
        holder.close()

    # A missing generation-lock path is UNKNOWN, not dead. The lookup cannot
    # omit that row without turning the live sibling into a false unique match,
    # so the default path refuses as unknown rather than adopting
    # only-kernel-live or reporting ambiguous identity. Forced is the hatch.
    assert lookup.sessions is None
    assert "dead-sql-active" in str(lookup.unreadable_reason)
    assert completed.returncode != 0, (completed.stdout, completed.stderr)
    assert not sentinel.exists()
    assert "controller identity is ambiguous" not in completed.stderr
    assert "controller is not registered" not in completed.stderr
    assert "controller registry could not be read" in completed.stderr
    assert "Retry the dispatch" in completed.stderr
    assert "--unregistered-forced" not in completed.stderr
    assert authority.read_all(
        "SELECT dispatch_id FROM dispatch_attempts WHERE dispatch_id = ?",
        (dispatch_id,),
    ) == []
    assert forced.returncode == 0, (forced.stdout, forced.stderr)
    assert forced_sentinel.is_file()
    assert "ownership could not be determined" in forced.stderr
    assert "controller is not registered" not in forced.stderr
    owner = _attempt_owner(authority, forced_id)
    assert owner["owner_controller_label"] is None
    assert owner["owner_session_digest"] is None


def test_unlocked_dead_lease_does_not_make_live_owner_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env, project, _advertised = _isolated_env(monkeypatch, tmp_path)
    authority = journal.open_or_create_journal(project)
    dead_principal = ledger.process_identity(os.getpid())
    assert dead_principal is not None
    dead_principal = {
        **dead_principal,
        "pid": 2_147_000_000,
        "start_token": "dead-controller",
    }
    dead = authority.claim_or_renew_lease(
        "dead-sql-active",
        principal=dead_principal,
    )
    assert dead.committed and dead.value is not None
    dead_holder = wake.register_lease_holder(
        project,
        controller_label="dead-sql-active",
        lease_nonce=dead.value.nonce,
    )
    dead_holder.close()
    authority, holder, nonce = _register_controller(
        project,
        env,
        label="only-kernel-live",
        export_identity=False,
    )
    dispatch_id = "unlocked-dead-row-ignored"
    sentinel = tmp_path / "unlocked-dead-row-worker.pid"
    try:
        completed = _run_dispatch(env, project, dispatch_id, sentinel)
    finally:
        holder.close()

    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert sentinel.is_file()
    owner = _attempt_owner(authority, dispatch_id)
    assert owner["owner_controller_label"] == "only-kernel-live"
    assert owner["owner_session_digest"] == wake.controller_session_digest(nonce)


def test_unique_live_controller_outside_ancestry_is_not_adopted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env, project, _advertised = _isolated_env(monkeypatch, tmp_path)
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    stop = context.Event()
    sibling = context.Process(
        target=_hold_sibling_controller,
        args=(project, ready, stop),
    )
    sibling.start()
    identity = ready.get(timeout=10)
    assert "error" not in identity, identity
    dispatch_id = "unrelated-unique-live-controller"
    sentinel = tmp_path / "unrelated-worker.pid"
    try:
        completed = _run_dispatch(env, project, dispatch_id, sentinel)
    finally:
        stop.set()
        sibling.join(timeout=10)

    assert sibling.exitcode == 0
    assert completed.returncode != 0
    assert not sentinel.exists()
    assert _capacity_leases(dispatch_id) == []
    assert "sibling-controller is kernel-live under pid" in completed.stderr
    assert "not in this invocation's process ancestry" in completed.stderr
    assert "--controller-session-id" not in completed.stderr


def test_registered_controller_label_and_pid_self_resolve_nonce(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env, project, _advertised = _isolated_env(monkeypatch, tmp_path)
    authority, holder, nonce = _register_controller(
        project,
        env,
        export_identity=False,
    )
    dispatch_id = "registered-controller-label-pid"
    sentinel = tmp_path / "label-pid-worker.pid"
    try:
        completed = _run_dispatch(
            env,
            project,
            dispatch_id,
            sentinel,
            controller_label="registered-test",
            controller_pid=os.getpid(),
        )
    finally:
        holder.close()

    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert sentinel.is_file()
    record = json.loads(ledger.record_path(dispatch_id, create=False).read_text())
    assert record["controller_session_id"] == nonce
    assert record["controller_pid"] == os.getpid()
    assert record["controller_label"] == "registered-test"
    assert _attempt_owner(authority, dispatch_id)["owner_controller_label"] == (
        "registered-test"
    )


def test_registered_controller_exact_triple_records_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env, project, _advertised = _isolated_env(monkeypatch, tmp_path)
    authority, holder, nonce = _register_controller(
        project,
        env,
        export_identity=False,
    )
    dispatch_id = "registered-controller-exact-triple"
    sentinel = tmp_path / "exact-triple-worker.pid"
    try:
        completed = _run_dispatch(
            env,
            project,
            dispatch_id,
            sentinel,
            controller_label="registered-test",
            controller_pid=os.getpid(),
            controller_session_id=nonce,
        )
    finally:
        holder.close()

    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert sentinel.is_file()
    record = json.loads(ledger.record_path(dispatch_id, create=False).read_text())
    assert record["controller_session_id"] == nonce
    assert record["controller_pid"] == os.getpid()
    assert record["controller_label"] == "registered-test"
    owner = _attempt_owner(authority, dispatch_id)
    assert owner["owner_controller_label"] == "registered-test"
    assert owner["owner_session_digest"] == wake.controller_session_digest(nonce)


def test_live_controller_with_wrong_nonce_refuses_with_exact_owned_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env, project, advertised = _isolated_env(monkeypatch, tmp_path)
    _authority, holder, nonce = _register_controller(
        project,
        env,
        export_identity=False,
    )
    dispatch_id = "registered-controller-wrong-nonce"
    sentinel = tmp_path / "wrong-nonce-worker.pid"
    try:
        completed = _run_dispatch(
            env,
            project,
            dispatch_id,
            sentinel,
            controller_label="registered-test",
            controller_pid=os.getpid(),
            controller_session_id="wrong-nonce",
        )
    finally:
        holder.close()

    assert completed.returncode != 0
    assert not sentinel.exists()
    assert "kernel-live controller exists" in completed.stderr
    lines = completed.stderr.splitlines()
    command_line = next(line for line in lines if "goalflight_dispatch.py" in line)
    command = shlex.split(command_line)
    assert command[:2] == [
        "python3",
        str(advertised / "scripts" / "goalflight_dispatch.py"),
    ]
    assert command[command.index("--controller-label") + 1] == "registered-test"
    assert command[command.index("--controller-pid") + 1] == str(os.getpid())
    assert command[command.index("--controller-session-id") + 1] == nonce
    assert "wrong-nonce" not in command
    assert str(ROOT) not in command_line


def test_live_controller_under_different_pid_is_not_adopted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env, project, _advertised = _isolated_env(monkeypatch, tmp_path)
    _authority, holder, _nonce = _register_controller(
        project,
        env,
        export_identity=False,
    )
    dispatch_id = "registered-controller-wrong-pid"
    sentinel = tmp_path / "wrong-pid-worker.pid"
    try:
        completed = _run_dispatch(
            env,
            project,
            dispatch_id,
            sentinel,
            controller_label="registered-test",
            controller_pid=os.getpid() + 1,
        )
    finally:
        holder.close()

    assert completed.returncode != 0
    assert not sentinel.exists()
    assert "registered-test is kernel-live under pid" in completed.stderr
    assert f"not requested pid {os.getpid() + 1}" in completed.stderr
    assert "refusing to adopt or displace" in completed.stderr


def test_correct_nonce_does_not_override_wrong_explicit_pid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env, project, _advertised = _isolated_env(monkeypatch, tmp_path)
    _authority, holder, nonce = _register_controller(
        project,
        env,
        export_identity=False,
    )
    dispatch_id = "registered-controller-correct-nonce-wrong-pid"
    sentinel = tmp_path / "correct-nonce-wrong-pid-worker.pid"
    requested_pid = os.getpid() + 1
    try:
        completed = _run_dispatch(
            env,
            project,
            dispatch_id,
            sentinel,
            controller_label="registered-test",
            controller_pid=requested_pid,
            controller_session_id=nonce,
        )
    finally:
        holder.close()

    assert completed.returncode != 0
    assert not sentinel.exists()
    assert _capacity_leases(dispatch_id) == []
    assert "registered-test is kernel-live under pid" in completed.stderr
    assert f"not requested pid {requested_pid}" in completed.stderr
    assert "refusing to adopt or displace" in completed.stderr


def test_multiple_kernel_live_controllers_refuse_without_guessing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env, project, _advertised = _isolated_env(monkeypatch, tmp_path)
    authority, first_holder, _first_nonce = _register_controller(
        project,
        env,
        label="controller-a",
        export_identity=False,
    )
    _same_authority, second_holder, _second_nonce = _register_controller(
        project,
        env,
        label="controller-b",
        export_identity=False,
    )
    dispatch_id = "ambiguous-live-controllers"
    sentinel = tmp_path / "ambiguous-worker.pid"
    try:
        completed = _run_dispatch(env, project, dispatch_id, sentinel)
    finally:
        second_holder.close()
        first_holder.close()

    assert completed.returncode != 0
    assert not sentinel.exists()
    assert _capacity_leases(dispatch_id) == []
    assert authority.read_all(
        "SELECT dispatch_id FROM dispatch_attempts WHERE dispatch_id = ?",
        (dispatch_id,),
    ) == []
    assert "controller identity is ambiguous" in completed.stderr
    assert "controller-a (pid" in completed.stderr
    assert "controller-b (pid" in completed.stderr
    assert "Refusing to guess an owner" in completed.stderr


def test_unregistered_controller_refuses_before_worker_or_capacity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env, project, advertised = _isolated_env(monkeypatch, tmp_path)
    dispatch_id = "unregistered-controller-refused"
    sentinel = tmp_path / "must-not-run.pid"

    completed = _run_dispatch(env, project, dispatch_id, sentinel)

    assert completed.returncode != 0, (completed.stdout, completed.stderr)
    assert not sentinel.exists(), "refused dispatch still launched its worker"
    assert not ledger.record_path(dispatch_id, create=False).exists()
    assert _capacity_leases(dispatch_id) == []
    pidfiles = tmp_path / "pidfiles"
    assert not pidfiles.exists() or list(pidfiles.iterdir()) == []

    lines = completed.stderr.splitlines()
    warning_index = next(
        index for index, line in enumerate(lines) if "controller is not registered" in line
    )
    command = shlex.join(
        [
            "python3",
            str(advertised / "scripts" / "goalflight_session_status.py"),
            "--controller-startup",
            "--controller-pid-from-ancestry",
        ]
    )
    assert lines[warning_index + 1] == command
    assert "--unregistered-forced" in lines[warning_index + 2]
    command_path = Path(shlex.split(lines[warning_index + 1])[1])
    assert command_path.is_absolute()
    assert "~" not in lines[warning_index + 1]
    assert str(ROOT) not in completed.stderr


def test_direct_acp_unregistered_refuses_before_capacity_or_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _env, project, advertised = _isolated_env(monkeypatch, tmp_path)
    dispatch_id = "direct-acp-unregistered"
    cfg = _direct_acp_cfg(project, tmp_path, dispatch_id)
    _stub_direct_acp_preflight(monkeypatch)
    capacity_calls: list[str] = []
    spawn_calls: list[str] = []

    async def acquire(*_args, **_kwargs):
        capacity_calls.append("capacity")
        return {"decision": "allow", "lease": {"lease_id": "must-not-lease"}}

    async def spawn(*_args, **_kwargs):
        spawn_calls.append("spawn")
        raise AssertionError("unregistered direct ACP reached worker spawn")

    monkeypatch.setattr(acp.goalflight_capacity, "acquire_with_wait_async", acquire)
    monkeypatch.setattr(acp, "spawn_and_handshake_with_retry", spawn)

    payload = asyncio.run(acp.run_acp_dispatch(cfg))

    assert payload["state"] == "blocked_controller_registration"
    assert payload["lease_id"] is None
    assert payload["worker_pid"] is None
    assert capacity_calls == []
    assert spawn_calls == []
    assert "controller is not registered" in str(payload["error"])
    assert str(advertised / "scripts" / "goalflight_session_status.py") in str(
        payload["error"]
    )
    assert not ledger.record_path(dispatch_id, create=False).exists()


def test_direct_acp_registered_controller_reaches_capacity_with_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env, project, _advertised = _isolated_env(monkeypatch, tmp_path)
    _authority, holder, nonce = _register_controller(
        project,
        env,
        label="direct-acp-controller",
        export_identity=False,
    )
    dispatch_id = "direct-acp-registered"
    cfg = _direct_acp_cfg(project, tmp_path, dispatch_id)
    _stub_direct_acp_preflight(monkeypatch)
    capacity_calls: list[str] = []

    async def block_capacity(*_args, **_kwargs):
        capacity_calls.append("capacity")
        return {"decision": "wait", "reason": "test_capacity_block"}

    monkeypatch.setattr(
        acp.goalflight_capacity,
        "acquire_with_wait_async",
        block_capacity,
    )
    try:
        payload = asyncio.run(acp.run_acp_dispatch(cfg))
    finally:
        holder.close()

    assert capacity_calls == ["capacity"]
    assert payload["state"] == "blocked_capacity"
    assert payload["controller_label"] == "direct-acp-controller"
    assert payload["controller_pid"] == os.getpid()
    assert payload["controller_session_id"] == nonce


def test_unregistered_forced_launches_with_null_owner_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env, project, _advertised = _isolated_env(monkeypatch, tmp_path)
    dispatch_id = "unregistered-controller-forced"
    sentinel = tmp_path / "forced-worker.pid"

    completed = _run_dispatch(
        env,
        project,
        dispatch_id,
        sentinel,
        forced=True,
    )

    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert sentinel.is_file()
    assert "controller is not registered" in completed.stderr
    assert "--unregistered-forced" in completed.stderr
    tail = (tmp_path / f"{dispatch_id}.tail").read_text(encoding="utf-8")
    assert "controller is not registered" in tail
    assert "--unregistered-forced" in tail
    record = json.loads(ledger.record_path(dispatch_id, create=False).read_text())
    assert record["controller_session_id"] is None
    assert record["controller_pid"] is None
    assert record["controller_label"] is None
    authority = journal.Journal(project)
    owner = _attempt_owner(authority, dispatch_id)
    assert owner["owner_controller_label"] is None
    assert owner["owner_session_digest"] is None


def test_help_documents_unregistered_override() -> None:
    completed = subprocess.run(
        [sys.executable, str(DISPATCH), "--help"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    assert completed.returncode == 0
    assert "--unregistered-forced" in completed.stdout
    assert "--controller-session-id" in completed.stdout

    resume_help = subprocess.run(
        [sys.executable, str(DISPATCH), "resume", "--help"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    assert resume_help.returncode == 0
    assert "--controller-label" in resume_help.stdout
    assert "--controller-pid" in resume_help.stdout
    assert "--controller-session-id" in resume_help.stdout
