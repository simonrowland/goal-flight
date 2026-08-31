#!/usr/bin/env python3
"""Focused regressions for tracked Codex rollout resume dispatches."""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import queue
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_codex_sessions as S  # noqa: E402
import goalflight_dispatch as D  # noqa: E402
import goalflight_journal as J  # noqa: E402
import goalflight_ledger as L  # noqa: E402
import goalflight_wake as wake  # noqa: E402
import goalflight_watch as W  # noqa: E402


SESSION_ID = "12345678-1234-4abc-8def-1234567890ab"


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Codex rollout resume is local POSIX-only",
)


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(state))
    monkeypatch.setenv("GOALFLIGHT_CODEX_STATE_DIR", str(state))
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE_DIR", str(tmp_path / "task-store"))
    monkeypatch.setenv("GOALFLIGHT_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("GOALFLIGHT_MESSAGES_DIR", str(tmp_path / "messages"))
    monkeypatch.setenv("GOALFLIGHT_WAKE_LEDGER_DIR", str(tmp_path / "wake-ledger"))
    monkeypatch.setenv("GOALFLIGHT_PIDFILE_DIR", str(tmp_path / "pidfiles"))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", "/dev/null")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_WAIT_S", "0")
    monkeypatch.setenv("GOALFLIGHT_DISABLE_NUDGES", "1")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("GOALFLIGHT_CODEX_CONTEXT_MODE", raising=False)
    for key in (
        "GOALFLIGHT_CONTROLLER_LABEL",
        "GOALFLIGHT_CONTROLLER_PID",
        "GOALFLIGHT_CONTROLLER_SESSION_ID",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE",
    ):
        monkeypatch.delenv(key, raising=False)


def _dispatch_home(tmp_path: Path, dispatch_id: str) -> Path:
    return tmp_path / "state" / "dispatch-homes" / dispatch_id


def _write_rollout(home: Path, session_id: str = SESSION_ID) -> Path:
    rollout = (
        home
        / "sessions"
        / "2026"
        / "07"
        / "28"
        / f"rollout-2026-07-28T12-00-00-{session_id}.jsonl"
    )
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
    return rollout


def _write_parent_record(
    tmp_path: Path,
    *,
    dispatch_id: str = "parent-dispatch",
    session_id: str | None = SESSION_ID,
    home: Path | None = None,
) -> dict:
    status_path = tmp_path / f"{dispatch_id}.status.json"
    record = {
        "schema": L.SCHEMA,
        "dispatch_id": dispatch_id,
        "agent": "codex",
        "engine": "codex",
        "shape": "bash",
        "account": "old-seat",
        "transport": "dispatch",
        "project_root": str(tmp_path),
        "worker_cwd": str(tmp_path),
        "status_path": str(status_path),
        "state": "blocked",
        "terminal_state": "blocked",
        "started_at": L.utc_now(),
        "task_ids": ["t-123"],
    }
    if session_id is not None:
        record["codex_session_id"] = session_id
    if home is not None:
        record["codex_home"] = str(home)
        record["codex_home_owner_dispatch_id"] = dispatch_id
    L.write_record(record)
    return record


def _stub_detached_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict], list[str]]:
    spawn_calls: list[dict] = []
    leases: list[str] = []
    monkeypatch.setattr(D, "_reap_quota_stuck_before_bash_launch", lambda: None)
    monkeypatch.setattr(
        D,
        "_acquire_capacity",
        lambda *_args, **_kwargs: leases.append("lease-resume") or "lease-resume",
    )
    monkeypatch.setattr(
        D,
        "_rebuild_codex_resume_home",
        lambda _root, _parent, expected_home, _session, **_kwargs: (
            str(expected_home),
            "new-seat",
        ),
    )
    monkeypatch.setattr(D, "_mark_queue_claim_launch_started", lambda _args: None)
    monkeypatch.setattr(
        D, "_mark_queue_claim_worker_spawn_intent", lambda _args: None
    )
    monkeypatch.setattr(
        D, "_mark_queue_claim_worker_spawned", lambda _args, _pid: None
    )
    monkeypatch.setattr(
        D,
        "_process_identity_after_spawn",
        lambda pid: {
            "pid": pid,
            "pgid": pid,
            "lstart": "Mon Jul 28 12:00:00 2026",
            "comm": "codex",
        },
    )
    monkeypatch.setattr(D, "process_group_id", lambda pid: pid)
    monkeypatch.setattr(
        D, "_start_caffeinate", lambda *_args, **_kwargs: (None, None)
    )
    monkeypatch.setattr(D, "_attach_worker_to_lease", lambda *_args: None)
    monkeypatch.setattr(D, "_detach_lease_to_worker", lambda *_args: None)
    monkeypatch.setattr(D, "_write_pidfile", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        D, "_export_dashboard_status_for_project", lambda *_args: None
    )
    monkeypatch.setattr(
        D, "_upsert_project_registry_for_dispatch", lambda *_args: None
    )
    monkeypatch.setattr(
        D, "_start_dashboard_refresh_for_project", lambda *_args: None
    )

    def spawn(argv: list[str], **kwargs) -> int:
        spawn_calls.append(
            {
                "argv": list(argv),
                "env": dict(kwargs.get("env") or {}),
                "stdin_path": kwargs.get("stdin_path"),
                "label": kwargs["label"],
            }
        )
        pid = 42000 + len(spawn_calls)
        return pid

    monkeypatch.setattr(D, "_spawn_daemonized_process", spawn)
    return spawn_calls, leases


def _stub_forked_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    home: Path,
) -> Path:
    """Use real ledger/home code while replacing provider and process launches."""
    markers = tmp_path / "process-markers"
    markers.mkdir(exist_ok=True)
    monkeypatch.setattr(D, "_reap_quota_stuck_before_bash_launch", lambda: None)

    def acquire(*_args, **_kwargs) -> str:
        (markers / f"{os.getpid()}-capacity").write_text("acquired")
        return f"lease-{os.getpid()}"

    def resolve(*_args, **_kwargs) -> tuple[str, str]:
        home.mkdir(parents=True, exist_ok=True)
        (home / "auth.json").write_text("new-seat", encoding="utf-8")
        return str(home), "new-seat"

    def spawn(argv: list[str], **kwargs) -> int:
        label = kwargs["label"]
        (markers / f"{os.getpid()}-{label}").write_text(
            json.dumps(list(argv)),
            encoding="utf-8",
        )
        pid = 500_000 + (os.getpid() % 10_000) + len(list(markers.iterdir()))
        return pid

    monkeypatch.setattr(D, "_acquire_capacity", acquire)
    monkeypatch.setattr(D, "resolve_codex_home", resolve)
    monkeypatch.setattr(D, "_spawn_daemonized_process", spawn)
    monkeypatch.setattr(D, "_mark_queue_claim_launch_started", lambda _args: None)
    monkeypatch.setattr(
        D, "_mark_queue_claim_worker_spawn_intent", lambda _args: None
    )
    monkeypatch.setattr(
        D, "_mark_queue_claim_worker_spawned", lambda _args, _pid: None
    )
    monkeypatch.setattr(
        D,
        "_process_identity_after_spawn",
        lambda pid: {
            "pid": pid,
            "pgid": pid,
            "lstart": "Mon Jul 28 12:00:00 2026",
            "comm": "codex",
        },
    )
    monkeypatch.setattr(D, "process_group_id", lambda pid: pid)
    monkeypatch.setattr(
        D, "_start_caffeinate", lambda *_args, **_kwargs: (None, None)
    )
    monkeypatch.setattr(D, "_attach_worker_to_lease", lambda *_args: None)
    monkeypatch.setattr(D, "_detach_lease_to_worker", lambda *_args: None)
    monkeypatch.setattr(D, "_write_pidfile", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        D, "_export_dashboard_status_for_project", lambda *_args: None
    )
    monkeypatch.setattr(
        D, "_upsert_project_registry_for_dispatch", lambda *_args: None
    )
    monkeypatch.setattr(
        D, "_start_dashboard_refresh_for_project", lambda *_args: None
    )
    return markers


def test_watcher_harvests_session_handle_into_status_and_ledger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dispatch_id = "harvest-session"
    home = _dispatch_home(tmp_path, dispatch_id)
    _write_rollout(home)
    _write_parent_record(
        tmp_path,
        dispatch_id=dispatch_id,
        session_id=None,
        home=home,
    )
    tail = tmp_path / "worker.tail"
    tail.write_text("", encoding="utf-8")
    status_path = tmp_path / "worker.status.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "goalflight_watch.py",
            "--pid",
            "99999999",
            "--tail",
            str(tail),
            "--status-json",
            str(status_path),
            "--dispatch-id",
            dispatch_id,
            "--agent",
            "codex",
            "--codex-dispatch-home-resolved",
            "--codex-dispatch-home",
            str(home),
            "--codex-home-owner-dispatch-id",
            dispatch_id,
            "--poll-secs",
            "0.01",
        ],
    )

    assert W.main() != 0
    status = json.loads(status_path.read_text(encoding="utf-8"))
    ledger = json.loads(L.record_path(dispatch_id).read_text(encoding="utf-8"))
    assert status["codex_session_id"] == SESSION_ID
    assert status["codex_home"] == str(home)
    assert status["codex_home_owner_dispatch_id"] == dispatch_id
    assert ledger["codex_session_id"] == SESSION_ID
    assert ledger["codex_home_owner_dispatch_id"] == dispatch_id
    assert home.is_dir(), "recorded sessions must survive terminal cleanup"


def test_handle_harvest_never_guesses_among_multiple_rollouts(
    tmp_path: Path,
) -> None:
    home = _dispatch_home(tmp_path, "ambiguous-sessions")
    _write_rollout(home)
    _write_rollout(home, "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")

    assert S.discover_session_id(home) is None


def test_resume_argv_places_flags_before_subcommand_and_feeds_prompt_via_stdin(
    tmp_path: Path,
) -> None:
    prompt = tmp_path / "revisions.md"
    prompt.write_text("Apply the reviewed revisions.", encoding="utf-8")
    args = SimpleNamespace(
        agent="codex",
        codex_session_id=SESSION_ID,
        parent_dispatch_id="parent-dispatch",
        cwd=str(tmp_path),
        model=None,
        os_sandbox=None,
        read_only=False,
    )

    argv, stdin_path = D.build_worker(args, str(prompt), [])

    resume_index = argv.index("resume")
    assert argv[:2] == ["codex", "exec"]
    assert argv.index("--skip-git-repo-check") < resume_index
    assert argv.index("--sandbox") < resume_index
    assert argv.index("-c") < resume_index
    assert argv.index("-C") < resume_index
    assert argv[resume_index:] == [
        "resume",
        SESSION_ID,
        "-",  # prompt via stdin: argv would truncate long revision lists
    ]
    # The prompt is fed from a file, so codex never blocks waiting on EOF and a
    # long revision list cannot overflow argv.
    assert stdin_path == str(prompt)


def test_resume_rebuild_allows_cross_seat_and_preserves_rollout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dispatch_id = "cross-seat-parent"
    home = _dispatch_home(tmp_path, dispatch_id)
    rollout = _write_rollout(home)
    (home / "auth.json").write_text("old-seat", encoding="utf-8")
    calls: list[tuple[Path, str | None, str]] = []

    def resolve(
        project_root: Path,
        explicit_account: str | None,
        resolved_dispatch_id: str,
    ) -> tuple[str, str]:
        calls.append((project_root, explicit_account, resolved_dispatch_id))
        home.mkdir(parents=True)
        (home / "auth.json").write_text("new-seat", encoding="utf-8")
        return str(home), "new-seat"

    monkeypatch.setattr(D, "resolve_codex_home", resolve)
    monkeypatch.setattr(D, "cleanup_codex_dispatch_home", lambda _dispatch_id: None)

    rebuilt, effective_account = D._rebuild_codex_resume_home(
        tmp_path,
        dispatch_id,
        home,
        SESSION_ID,
    )

    assert calls == [(tmp_path, None, dispatch_id)]
    assert rebuilt == str(home)
    assert effective_account == "new-seat"
    assert (home / "auth.json").read_text(encoding="utf-8") == "new-seat"
    assert S.rollout_path(home, SESSION_ID) == rollout


def test_failed_seat_rebuild_restores_original_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dispatch_id = "restore-parent"
    home = _dispatch_home(tmp_path, dispatch_id)
    _write_rollout(home)
    (home / "auth.json").write_text("old-seat", encoding="utf-8")

    def fail_resolve(
        _project_root: Path,
        explicit_account: str | None,
        _dispatch_id: str,
    ) -> tuple[None, None]:
        assert explicit_account is None
        return None, None

    monkeypatch.setattr(D, "resolve_codex_home", fail_resolve)
    monkeypatch.setattr(D, "cleanup_codex_dispatch_home", lambda _dispatch_id: None)

    with pytest.raises(D.DispatchUsageError) as exc_info:
        D._rebuild_codex_resume_home(
            tmp_path,
            dispatch_id,
            home,
            SESSION_ID,
        )

    assert str(exc_info.value) == (
        "could not rebuild dispatch home for restore-parent with a healthy codex seat"
    )
    assert (home / "auth.json").read_text(encoding="utf-8") == "old-seat"
    assert S.rollout_path(home, SESSION_ID) is not None


def test_resume_verb_passes_lineage_and_tasks_to_normal_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent_id = "verb-parent"
    home = _dispatch_home(tmp_path, parent_id)
    _write_rollout(home)
    _write_parent_record(tmp_path, dispatch_id=parent_id, home=home)
    prompt = tmp_path / "revisions.md"
    prompt.write_text("Revise the implementation.", encoding="utf-8")
    captured: list[list[str]] = []
    monkeypatch.setattr(
        D,
        "_reserve_auto_dispatch_id",
        lambda _agent, _base: "codex-resume-child",
    )
    monkeypatch.setattr(
        D,
        "main",
        lambda argv=None: captured.append(list(argv or [])) or 0,
    )

    assert D._cmd_resume(
        [
            parent_id,
            "--prompt-file",
            str(prompt),
            "--unregistered-forced",
            "--controller-label",
            "resume-test",
            "--controller-pid",
            "12345",
            "--controller-session-id",
            "resume-test-nonce",
        ]
    ) == 0

    launch = captured[0]
    assert launch[launch.index("--dispatch-id") + 1] == "codex-resume-child"
    assert launch[launch.index("--parent-dispatch-id") + 1] == parent_id
    assert launch[launch.index("--codex-session-id") + 1] == SESSION_ID
    assert launch[launch.index("--engine-session-id") + 1] == SESSION_ID
    assert launch[launch.index("--codex-resume-home") + 1] == str(home)
    assert (
        launch[launch.index("--codex-home-owner-dispatch-id") + 1]
        == parent_id
    )
    assert launch[launch.index("--task") + 1] == "t-123"
    assert "--unregistered-forced" in launch
    assert launch[launch.index("--controller-label") + 1] == "resume-test"
    assert launch[launch.index("--controller-pid") + 1] == "12345"
    assert launch[launch.index("--controller-session-id") + 1] == (
        "resume-test-nonce"
    )
    assert "--account" not in launch


def test_resume_by_single_registered_controller_records_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent_id = "owned-resume-parent"
    child_id = "owned-resume-child"
    home = _dispatch_home(tmp_path, parent_id)
    _write_rollout(home)
    _write_parent_record(tmp_path, dispatch_id=parent_id, home=home)
    prompt = tmp_path / "owned-resume.md"
    prompt.write_text("Continue the registered turn.", encoding="utf-8")
    authority = J.open_or_create_journal(tmp_path)
    principal = L.process_identity(os.getpid())
    assert principal is not None
    claimed = authority.claim_or_renew_lease(
        "resume-controller",
        principal=principal,
    )
    assert claimed.committed and claimed.value is not None
    holder = wake.register_lease_holder(
        tmp_path,
        controller_label="resume-controller",
        lease_nonce=claimed.value.nonce,
    )
    monkeypatch.setattr(
        D,
        "_reserve_auto_dispatch_id",
        lambda _agent, _base: child_id,
    )
    _spawn_calls, leases = _stub_detached_runtime(monkeypatch)

    try:
        rc = D._cmd_resume([parent_id, "--prompt-file", str(prompt)])
    finally:
        holder.close()

    assert rc == 0
    assert leases == ["lease-resume"]
    record = json.loads(L.record_path(child_id).read_text(encoding="utf-8"))
    assert record["controller_label"] == "resume-controller"
    assert record["controller_session_id"] == claimed.value.nonce
    owner = authority.read_all(
        """SELECT owner_controller_label, owner_session_digest
           FROM dispatch_attempts WHERE dispatch_id = ?""",
        (child_id,),
    )[0]
    assert owner["owner_controller_label"] == "resume-controller"
    assert owner["owner_session_digest"] == wake.controller_session_digest(
        claimed.value.nonce
    )


def test_resumed_turn_uses_normal_tracking_surfaces(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent_id = "tracked-parent"
    child_id = "tracked-child"
    home = _dispatch_home(tmp_path, parent_id)
    _write_rollout(home)
    _write_parent_record(tmp_path, dispatch_id=parent_id, home=home)
    prompt = tmp_path / "revisions.md"
    prompt.write_text("Apply revision one.", encoding="utf-8")
    tail = tmp_path / "child.tail"
    status_path = tmp_path / "child.status.json"
    spawn_calls, leases = _stub_detached_runtime(monkeypatch)

    rc = D.main(
        [
            "--agent",
            "codex",
            "--unregistered-forced",
            "--shape",
            "bash",
            "--dispatch-id",
            child_id,
            "--cwd",
            str(tmp_path),
            "--prompt-file",
            str(prompt),
            "--tail",
            str(tail),
            "--status-json",
            str(status_path),
            "--parent-dispatch-id",
            parent_id,
            "--codex-session-id",
            SESSION_ID,
            "--codex-resume-home",
            str(home),
            "--codex-home-owner-dispatch-id",
            parent_id,
            "--launch-detached",
        ]
    )

    assert rc == 0
    assert leases == ["lease-resume"]
    worker = next(call for call in spawn_calls if call["label"] == "worker")
    watcher = next(call for call in spawn_calls if call["label"] == "watcher")
    assert worker["env"]["CODEX_HOME"] == str(home)
    assert worker["stdin_path"] is not None  # prompt fed from file, not argv
    assert worker["argv"][worker["argv"].index("resume") + 1] == SESSION_ID
    assert (
        watcher["argv"][watcher["argv"].index("--codex-dispatch-home") + 1]
        == str(home)
    )
    assert "--codex-session-id" in watcher["argv"]
    assert "--parent-dispatch-id" in watcher["argv"]

    ledger = json.loads(L.record_path(child_id).read_text(encoding="utf-8"))
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert ledger["state"] == "running"
    assert ledger["lease_id"] == "lease-resume"
    assert ledger["parent_dispatch_id"] == parent_id
    assert ledger["codex_session_id"] == SESSION_ID
    assert ledger["codex_home"] == str(home)
    assert ledger["codex_home_owner_dispatch_id"] == parent_id
    assert ledger["effective_account"] == "new-seat"
    assert status["state"] == "starting"
    assert status["parent_dispatch_id"] == parent_id
    assert status["codex_session_id"] == SESSION_ID
    assert status["codex_home"] == str(home)
    assert status["codex_home_owner_dispatch_id"] == parent_id
    aggregate = next(
        row
        for row in L.status_payload()["records"]
        if row["dispatch_id"] == child_id
    )
    assert aggregate["parent_dispatch_id"] == parent_id
    assert aggregate["codex_session_id"] == SESSION_ID
    assert aggregate["codex_home"] == str(home)
    assert aggregate["codex_home_owner_dispatch_id"] == parent_id


def test_concurrent_resumes_claim_before_capacity_and_only_one_launches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two processes pass the old check; one claims and holds the replace lock."""
    parent_id = "concurrent-parent"
    home = _dispatch_home(tmp_path, parent_id)
    _write_rollout(home)
    _write_parent_record(tmp_path, dispatch_id=parent_id, home=home)
    prompt = tmp_path / "revisions.md"
    prompt.write_text("Apply the same revision once.", encoding="utf-8")
    markers = _stub_forked_runtime(monkeypatch, tmp_path, home)
    ctx = mp.get_context("fork")

    original_validate = D._validate_codex_resume_source
    initial_barrier = ctx.Barrier(2)
    claim_entries = ctx.Value("i", 0)
    first_claim_validation = ctx.Event()
    second_claim_validation = ctx.Event()
    release_first_claim = ctx.Event()
    before_replace = ctx.Event()
    release_replace = ctx.Event()
    validation_calls = 0

    def synchronized_validate(*args, **kwargs):
        nonlocal validation_calls
        result = original_validate(*args, **kwargs)
        validation_calls += 1
        if validation_calls == 1:
            initial_barrier.wait(timeout=5)
        return result

    def claim_validated(*_args) -> None:
        with claim_entries.get_lock():
            claim_entries.value += 1
            entry = claim_entries.value
        if entry == 1:
            first_claim_validation.set()
            assert release_first_claim.wait(timeout=5)
        else:
            second_claim_validation.set()

    def replace_boundary(*_args) -> None:
        before_replace.set()
        assert release_replace.wait(timeout=5)

    monkeypatch.setattr(D, "_validate_codex_resume_source", synchronized_validate)
    monkeypatch.setattr(D, "_CODEX_RESUME_CLAIM_VALIDATED_HOOK", claim_validated)
    monkeypatch.setattr(D, "_CODEX_RESUME_BEFORE_REPLACE_HOOK", replace_boundary)
    monkeypatch.setattr(
        D,
        "_reserve_auto_dispatch_id",
        lambda _agent, _base: f"resume-child-{os.getpid()}",
    )

    results = ctx.Queue()

    def run_resume() -> None:
        results.put(
            (
                os.getpid(),
                D._cmd_resume(
                    [
                        parent_id,
                        "--prompt-file",
                        str(prompt),
                        "--unregistered-forced",
                    ]
                ),
            )
        )

    processes = [
        ctx.Process(target=run_resume),
        ctx.Process(target=run_resume),
    ]
    for process in processes:
        process.start()
    assert first_claim_validation.wait(timeout=5)
    claim_is_interprocess = not second_claim_validation.wait(timeout=0.5)
    release_first_claim.set()
    assert before_replace.wait(timeout=5)

    loser_pid, loser_rc = results.get(timeout=5)
    assert loser_rc == 64

    probe_results = ctx.Queue()

    def probe_replace_lock() -> None:
        with D._codex_resume_lock(home, SESSION_ID):
            probe_results.put("acquired")

    probe = ctx.Process(target=probe_replace_lock)
    probe.start()
    try:
        probe_results.get(timeout=0.5)
        replace_was_locked = False
    except queue.Empty:
        replace_was_locked = True

    release_replace.set()
    if replace_was_locked:
        assert probe_results.get(timeout=5) == "acquired"
    winner_pid, winner_rc = results.get(timeout=5)
    assert winner_rc == 0
    for process in [*processes, probe]:
        process.join(timeout=5)
        assert not process.is_alive()
        assert process.exitcode == 0

    assert claim_is_interprocess, (
        "owner-home/session claim validation must use an inter-process lock"
    )
    assert replace_was_locked, "the owner-home/session lock must cover replace"
    assert winner_pid != loser_pid
    assert len(list(markers.glob("*-capacity"))) == 1
    assert len(list(markers.glob("*-worker"))) == 1
    assert len(list(markers.glob("*-watcher"))) == 1
    child_records = [
        path
        for path in (L.record_path(f"resume-child-{process.pid}") for process in processes)
        if path.exists()
    ]
    assert len(child_records) == 1


def test_dead_preclaim_is_reconciled_before_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A hard-killed claimant cannot strand the Codex rollout lineage."""
    parent_id = "crash-parent"
    home = _dispatch_home(tmp_path, parent_id)
    _write_rollout(home)
    _write_parent_record(tmp_path, dispatch_id=parent_id, home=home)
    prompt = tmp_path / "revisions.md"
    prompt.write_text("Retry after the claimant dies.", encoding="utf-8")
    _stub_forked_runtime(monkeypatch, tmp_path, home)
    ctx = mp.get_context("fork")
    durable_claimed = ctx.Event()

    def pause_after_durable_claim(*_args) -> None:
        durable_claimed.set()
        ctx.Event().wait(timeout=30)

    monkeypatch.setattr(
        D,
        "_CODEX_RESUME_DURABLE_CLAIM_HOOK",
        pause_after_durable_claim,
    )
    monkeypatch.setattr(
        D,
        "_reserve_auto_dispatch_id",
        lambda _agent, _base: f"resume-child-{os.getpid()}",
    )

    claimant = ctx.Process(
        target=lambda: D._cmd_resume(
            [
                parent_id,
                "--prompt-file",
                str(prompt),
                "--unregistered-forced",
            ]
        )
    )
    claimant.start()
    assert durable_claimed.wait(timeout=5)
    stale_id = f"resume-child-{claimant.pid}"
    assert L.record_path(stale_id).is_file()
    claimant.kill()
    claimant.join(timeout=5)
    assert not claimant.is_alive()

    monkeypatch.setattr(D, "_CODEX_RESUME_DURABLE_CLAIM_HOOK", None)
    assert D._cmd_resume(
        [parent_id, "--prompt-file", str(prompt), "--unregistered-forced"]
    ) == 0

    stale = json.loads(L.record_path(stale_id).read_text(encoding="utf-8"))
    assert stale["state"] == "failed"
    assert stale["terminal_state"] == "error"
    assert str(stale["reason"]).startswith("stale_codex_resume_preclaim:")
    retry = json.loads(
        L.record_path(f"resume-child-{os.getpid()}").read_text(encoding="utf-8")
    )
    assert retry["state"] == "running"


def test_parent_child_grandchild_resume_preserves_original_home_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent_id = "resume-parent"
    child_id = "resume-child"
    grandchild_id = "resume-grandchild"
    home = _dispatch_home(tmp_path, parent_id)
    _write_rollout(home)
    _write_parent_record(tmp_path, dispatch_id=parent_id, home=home)
    prompt = tmp_path / "revisions.md"
    prompt.write_text("Apply the next reviewed revision.", encoding="utf-8")
    dispatch_base = tmp_path / "dispatch"
    dispatch_base.mkdir()

    _stub_detached_runtime(monkeypatch)
    reserved_ids = iter((child_id, grandchild_id))
    monkeypatch.setattr(
        D,
        "_reserve_auto_dispatch_id",
        lambda _agent, _base: next(reserved_ids),
    )
    monkeypatch.setattr(D, "_dispatch_base_dir", lambda: dispatch_base)

    assert D._cmd_resume(
        [parent_id, "--prompt-file", str(prompt), "--unregistered-forced"]
    ) == 0
    child = json.loads(L.record_path(child_id).read_text(encoding="utf-8"))
    assert child["parent_dispatch_id"] == parent_id
    assert child["codex_home"] == str(home)
    assert child["codex_home_owner_dispatch_id"] == parent_id

    child.update(
        {
            "state": "blocked",
            "terminal_state": "blocked",
            "worker_pid": None,
            "worker_identity": None,
        }
    )
    L.write_record(child)

    assert D._cmd_resume(
        [child_id, "--prompt-file", str(prompt), "--unregistered-forced"]
    ) == 0
    grandchild = json.loads(
        L.record_path(grandchild_id).read_text(encoding="utf-8")
    )
    assert grandchild["parent_dispatch_id"] == child_id
    assert grandchild["codex_session_id"] == SESSION_ID
    assert grandchild["codex_home"] == str(home)
    assert grandchild["codex_home_owner_dispatch_id"] == parent_id


@pytest.mark.parametrize(
    "case",
    [
        "missing_handle",
        "missing_home",
        "missing_rollout",
    ],
)
def test_resume_fails_honestly_without_fresh_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    parent_id = "failure-parent"
    home = _dispatch_home(tmp_path, parent_id)
    session_id = None if case == "missing_handle" else SESSION_ID
    if case in {"missing_handle", "missing_rollout"}:
        home.mkdir(parents=True)
    if case == "missing_handle":
        _write_rollout(home)
    _write_parent_record(
        tmp_path,
        dispatch_id=parent_id,
        session_id=session_id,
        home=home,
    )
    prompt = tmp_path / "revisions.md"
    prompt.write_text("Apply revisions.", encoding="utf-8")
    monkeypatch.setattr(
        D,
        "_reserve_auto_dispatch_id",
        lambda *_args, **_kwargs: pytest.fail(
            "honest failure must not allocate a fresh dispatch"
        ),
    )

    rc = D.main(["resume", parent_id, "--prompt-file", str(prompt)])

    assert rc == 64
    expected = {
        "missing_handle": (
            "goalflight_dispatch: dispatch failure-parent has no recorded "
            "codex session handle\n"
        ),
        "missing_home": (
            f"goalflight_dispatch: dispatch home missing for failure-parent: {home}\n"
        ),
        "missing_rollout": (
            "goalflight_dispatch: rollout missing for dispatch failure-parent: "
            f"session {SESSION_ID} under {home / 'sessions'}\n"
        ),
    }[case]
    assert capsys.readouterr().err == expected


@pytest.mark.parametrize(
    ("identity_result", "expected"),
    [
        (
            (True, "live"),
            "goalflight_dispatch: dispatch live-parent is still live; "
            "wait for terminal before resume\n",
        ),
        (
            (True, "identity_indeterminate"),
            "goalflight_dispatch: dispatch live-parent liveness is indeterminate; "
            "refusing resume\n",
        ),
    ],
)
def test_resume_refuses_live_or_indeterminate_source_with_exact_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    identity_result: tuple[bool, str],
    expected: str,
) -> None:
    parent_id = "live-parent"
    home = _dispatch_home(tmp_path, parent_id)
    _write_rollout(home)
    record = _write_parent_record(
        tmp_path,
        dispatch_id=parent_id,
        home=home,
    )
    record.update(
        {
            "state": "running",
            "terminal_state": "unknown",
            "worker_pid": 43210,
            "worker_identity": {
                "pid": 43210,
                "lstart": "Mon Jul 28 12:00:00 2026",
                "comm": "codex",
            },
        }
    )
    L.write_record(record)
    prompt = tmp_path / "revisions.md"
    prompt.write_text("Apply revisions.", encoding="utf-8")
    monkeypatch.setattr(L, "identity_matches", lambda _record: identity_result)
    monkeypatch.setattr(
        D,
        "_reserve_auto_dispatch_id",
        lambda *_args, **_kwargs: pytest.fail(
            "live-source refusal must not allocate a child dispatch"
        ),
    )

    rc = D.main(["resume", parent_id, "--prompt-file", str(prompt)])

    assert rc == 64
    assert capsys.readouterr().err == expected


def test_resume_refuses_existing_nonterminal_child_for_same_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parent_id = "duplicate-parent"
    child_id = "duplicate-child"
    home = _dispatch_home(tmp_path, parent_id)
    _write_rollout(home)
    _write_parent_record(tmp_path, dispatch_id=parent_id, home=home)
    child = _write_parent_record(
        tmp_path,
        dispatch_id=child_id,
        home=home,
    )
    child.update(
        {
            "state": "running",
            "terminal_state": "unknown",
            "parent_dispatch_id": parent_id,
            "codex_home_owner_dispatch_id": parent_id,
        }
    )
    L.write_record(child)
    prompt = tmp_path / "revisions.md"
    prompt.write_text("Apply revisions.", encoding="utf-8")
    monkeypatch.setattr(
        D,
        "_reserve_auto_dispatch_id",
        lambda *_args, **_kwargs: pytest.fail(
            "duplicate-child refusal must not allocate another child"
        ),
    )

    rc = D.main(["resume", parent_id, "--prompt-file", str(prompt)])

    assert rc == 64
    assert capsys.readouterr().err == (
        "goalflight_dispatch: dispatch duplicate-parent already has non-terminal "
        f"resume child duplicate-child for session {SESSION_ID}\n"
    )


def _from_queue_resume_argv(
    *,
    child_id: str,
    parent_id: str,
    prompt: Path,
    cwd: Path,
    home: Path,
    tail: Path,
    status_path: Path,
) -> list[str]:
    """Drain relaunch of an already-queued resume envelope."""
    return [
        "--agent",
        "codex",
        "--unregistered-forced",
        "--shape",
        "bash",
        "--dispatch-id",
        child_id,
        "--cwd",
        str(cwd),
        "--prompt-file",
        str(prompt),
        "--tail",
        str(tail),
        "--status-json",
        str(status_path),
        "--parent-dispatch-id",
        parent_id,
        "--codex-session-id",
        SESSION_ID,
        "--codex-resume-home",
        str(home),
        "--codex-home-owner-dispatch-id",
        parent_id,
        "--from-queue",
        "--launch-detached",
    ]


def _write_resume_child_record(
    tmp_path: Path,
    *,
    dispatch_id: str,
    parent_id: str,
    home: Path,
    state: str,
    worker_cwd: Path | None = None,
    worker_pid: int | None = None,
) -> dict:
    record = _write_parent_record(
        tmp_path,
        dispatch_id=dispatch_id,
        home=home,
    )
    record.update(
        {
            "state": state,
            "terminal_state": "unknown",
            "parent_dispatch_id": parent_id,
            "codex_home_owner_dispatch_id": parent_id,
            "worker_pid": worker_pid,
        }
    )
    if worker_cwd is not None:
        record["worker_cwd"] = str(worker_cwd)
    L.write_record(record)
    return record


def test_queued_resume_envelope_launches_when_only_nonterminal_child_is_itself(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Drain relaunch must not treat the envelope being launched as a duplicate."""
    parent_id = "selfblock-parent"
    child_id = "selfblock-queued"
    home = _dispatch_home(tmp_path, parent_id)
    _write_rollout(home)
    _write_parent_record(tmp_path, dispatch_id=parent_id, home=home)
    _write_resume_child_record(
        tmp_path,
        dispatch_id=child_id,
        parent_id=parent_id,
        home=home,
        state="queued",
    )
    prompt = tmp_path / "revisions.md"
    prompt.write_text("Continue the queued resume.", encoding="utf-8")
    tail = tmp_path / "queued.tail"
    status_path = tmp_path / "queued.status.json"
    spawn_calls, leases = _stub_detached_runtime(monkeypatch)

    rc = D.main(
        _from_queue_resume_argv(
            child_id=child_id,
            parent_id=parent_id,
            prompt=prompt,
            cwd=tmp_path,
            home=home,
            tail=tail,
            status_path=status_path,
        )
    )

    assert rc == 0
    assert leases == ["lease-resume"]
    assert any(call["label"] == "worker" for call in spawn_calls)
    ledger = json.loads(L.record_path(child_id).read_text(encoding="utf-8"))
    assert ledger["state"] == "running"
    assert ledger["parent_dispatch_id"] == parent_id
    assert ledger["codex_session_id"] == SESSION_ID


def test_queued_resume_envelope_refuses_different_live_resume_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Excluding the mid-launch envelope must not admit a second live resume."""
    parent_id = "selfblock-parent"
    child_id = "selfblock-queued"
    other_id = "selfblock-zzz-live"
    home = _dispatch_home(tmp_path, parent_id)
    _write_rollout(home)
    _write_parent_record(tmp_path, dispatch_id=parent_id, home=home)
    _write_resume_child_record(
        tmp_path,
        dispatch_id=child_id,
        parent_id=parent_id,
        home=home,
        state="queued",
    )
    other_cwd = tmp_path / "other-tree"
    other_cwd.mkdir()
    _write_resume_child_record(
        tmp_path,
        dispatch_id=other_id,
        parent_id=parent_id,
        home=home,
        state="running",
        worker_cwd=other_cwd,
        worker_pid=43211,
    )
    prompt = tmp_path / "revisions.md"
    prompt.write_text("Do not share this session.", encoding="utf-8")
    tail = tmp_path / "queued.tail"
    status_path = tmp_path / "queued.status.json"
    spawn_calls, _leases = _stub_detached_runtime(monkeypatch)

    rc = D.main(
        _from_queue_resume_argv(
            child_id=child_id,
            parent_id=parent_id,
            prompt=prompt,
            cwd=tmp_path,
            home=home,
            tail=tail,
            status_path=status_path,
        )
    )

    assert rc == 64
    assert spawn_calls == []
    err = capsys.readouterr().err
    assert (
        "goalflight_dispatch: dispatch selfblock-parent already has non-terminal "
        f"resume child {other_id} for session {SESSION_ID}"
    ) in err
    assert f"resume child {child_id} " not in err


def test_main_reports_resume_build_usage_error_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompt = tmp_path / "revisions.md"
    prompt.write_text("Apply revisions.", encoding="utf-8")

    rc = D.main(
        [
            "--agent",
            "codex",
            "--unregistered-forced",
            "--shape",
            "bash",
            "--dispatch-id",
            "invalid-resume-child",
            "--cwd",
            str(tmp_path),
            "--prompt-file",
            str(prompt),
            "--parent-dispatch-id",
            "invalid-resume-parent",
        ]
    )

    assert rc == 64
    assert capsys.readouterr().err == (
        "goalflight_dispatch: codex resume launch requires a recorded session handle\n"
    )


def test_main_reports_resume_rebuild_usage_error_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parent_id = "rebuild-parent"
    child_id = "rebuild-child"
    home = _dispatch_home(tmp_path, parent_id)
    _write_rollout(home)
    _write_parent_record(tmp_path, dispatch_id=parent_id, home=home)
    prompt = tmp_path / "revisions.md"
    prompt.write_text("Apply revisions.", encoding="utf-8")
    _stub_detached_runtime(monkeypatch)

    def fail_rebuild(*_args, **_kwargs):
        raise D.DispatchUsageError("resume home rebuild refused")

    monkeypatch.setattr(D, "_rebuild_codex_resume_home", fail_rebuild)

    rc = D.main(
        [
            "--agent",
            "codex",
            "--unregistered-forced",
            "--shape",
            "bash",
            "--dispatch-id",
            child_id,
            "--cwd",
            str(tmp_path),
            "--prompt-file",
            str(prompt),
            "--parent-dispatch-id",
            parent_id,
            "--codex-session-id",
            SESSION_ID,
            "--codex-resume-home",
            str(home),
            "--codex-home-owner-dispatch-id",
            parent_id,
            "--launch-detached",
        ]
    )

    assert rc == 64
    error = capsys.readouterr().err
    assert "controller not connected; reconnect as:" in error
    assert "--session-label" in error
    assert "--takeover" not in error
    assert "goalflight_dispatch: resume home rebuild refused" in error
    assert "Traceback" not in error


def test_blocked_capacity_resume_status_preserves_full_lineage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent_id = "capacity-parent"
    child_id = "capacity-child"
    home = _dispatch_home(tmp_path, parent_id)
    status_path = tmp_path / "capacity.status.json"
    waiting_payloads: list[dict] = []
    args = SimpleNamespace(
        agent="codex",
        dispatch_id=child_id,
        shape="bash",
        task_ids=["t-123"],
        parent_dispatch_id=parent_id,
        codex_session_id=SESSION_ID,
        codex_resume_home=str(home),
        codex_home_owner_dispatch_id=parent_id,
        max_idle_secs=300,
        priority="normal",
        controller_pid=None,
        capacity_wait_s=0,
    )

    def deny_capacity(_args, *, on_wait, **_kwargs):
        on_wait(1, 0.0, {"reason": "agent_worker_cap"})
        waiting_payloads.append(
            json.loads(status_path.read_text(encoding="utf-8"))
        )
        return {"decision": "deny", "reason": "agent_worker_cap"}

    monkeypatch.setattr(
        D.goalflight_capacity,
        "acquire_with_wait",
        deny_capacity,
    )

    with pytest.raises(SystemExit) as exc_info:
        D._acquire_capacity(
            args,
            project_root=tmp_path,
            status_json=status_path,
        )

    assert exc_info.value.code == 2
    blocked = json.loads(status_path.read_text(encoding="utf-8"))
    for payload, state in (
        (waiting_payloads[0], "waiting_capacity"),
        (blocked, "blocked_capacity"),
    ):
        assert payload["state"] == state
        assert payload["parent_dispatch_id"] == parent_id
        assert payload["codex_session_id"] == SESSION_ID
        assert payload["codex_home"] == str(home)
        assert payload["codex_home_owner_dispatch_id"] == parent_id
