#!/usr/bin/env python3
"""Focused regressions for tracked Codex rollout resume dispatches."""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import queue
import subprocess
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
import goalflight_worktree_pool as WP  # noqa: E402


SESSION_ID = "12345678-1234-4abc-8def-1234567890ab"
CANONICAL_SESSION_ID = "01a09067-25ec-7250-9872-3a277abe5716"
OTHER_CANONICAL_SESSION_ID = "01a09093-d6c7-7d72-b548-faaf31824d3f"


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
        "GOALFLIGHT_WORKTREE_LOCK_FD",
        "GOALFLIGHT_OCCUPANCY_LOCK_FD",
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


def _write_measured_rollout(home: Path, session_id: str, timestamp: str) -> Path:
    rollout = (
        home
        / "sessions"
        / "2026"
        / "09"
        / "11"
        / f"rollout-{timestamp}-{session_id}.jsonl"
    )
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
    return rollout


def _canonical_codex_home(tmp_path: Path, account: str) -> Path:
    return tmp_path / "home" / ".goal-flight" / "accounts" / account / "codex"


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
    monkeypatch.setattr(D.goalflight_capacity, "mark_lease_spawning", lambda _lease_id: True)
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
    monkeypatch.setattr(D.goalflight_capacity, "mark_lease_spawning", lambda _lease_id: True)
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


def test_shared_home_harvest_uses_this_dispatch_banner_not_newest_rollout(
    tmp_path: Path,
) -> None:
    home = _canonical_codex_home(tmp_path, "d78343")
    wanted = _write_measured_rollout(
        home,
        CANONICAL_SESSION_ID,
        "2026-09-11T08-17-54",
    )
    newer = _write_measured_rollout(
        home,
        OTHER_CANONICAL_SESSION_ID,
        "2026-09-11T09-06-40",
    )
    os.utime(wanted, (1_789_126_674, 1_789_126_674))
    os.utime(newer, (1_789_129_600, 1_789_129_600))
    tail = tmp_path / "codex-12150-1789129071.tail"
    tail.write_text(
        "OpenAI Codex v0.137.0\n"
        "--------\n"
        "workdir: /Users/simonrowland/Repos/goal-flight\n"
        "model: gpt-5.5\n"
        "provider: openai\n"
        "approval: never\n"
        "sandbox: workspace-write [workdir, /tmp, $TMPDIR]\n"
        "reasoning effort: xhigh\n"
        "reasoning summaries: none\n"
        "session id: 01a09067-25ec-7250-9872-3a277abe5716\n"
        "--------\n"
        "user\n",
        encoding="utf-8",
    )

    assert S.session_id_from_tail(home, tail) == CANONICAL_SESSION_ID
    tail.write_text(
        tail.read_text(encoding="utf-8").replace(
            CANONICAL_SESSION_ID,
            "01a09094-ffff-7d72-b548-faaf31824d3f",
        ),
        encoding="utf-8",
    )
    assert S.session_id_from_tail(home, tail) is None


def test_canonical_home_launch_harvests_handle_and_validates_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dispatch_id = "codex-12150-1789129071"
    account = "d78343"
    home = _canonical_codex_home(tmp_path, account)
    home.mkdir(parents=True)
    prompt = tmp_path / "brief-b359.md"
    prompt.write_text("Implement b-359.\n", encoding="utf-8")
    tail = tmp_path / f"{dispatch_id}.tail"
    status_path = tmp_path / f"{dispatch_id}.status.json"
    spawn_calls, leases = _stub_detached_runtime(monkeypatch)
    monkeypatch.setattr(D, "_codex_seat_api", lambda: None)

    rc = D.main(
        [
            "--agent",
            "codex",
            "--account",
            account,
            "--unregistered-forced",
            "--shape",
            "bash",
            "--dispatch-id",
            dispatch_id,
            "--cwd",
            str(tmp_path),
            "--prompt-file",
            str(prompt),
            "--tail",
            str(tail),
            "--status-json",
            str(status_path),
            "--launch-detached",
        ]
    )

    assert rc == 0
    assert leases == ["lease-resume"]
    worker = next(call for call in spawn_calls if call["label"] == "worker")
    watcher = next(call for call in spawn_calls if call["label"] == "watcher")
    assert worker["env"]["CODEX_HOME"] == str(home)
    assert (
        watcher["argv"][watcher["argv"].index("--codex-dispatch-home") + 1]
        == str(home)
    )
    launch_record = json.loads(L.record_path(dispatch_id).read_text(encoding="utf-8"))
    assert launch_record["codex_home"] == str(home)
    assert launch_record["effective_account"] == account

    _write_measured_rollout(
        home,
        CANONICAL_SESSION_ID,
        "2026-09-11T08-17-54",
    )
    _write_measured_rollout(
        home,
        OTHER_CANONICAL_SESSION_ID,
        "2026-09-11T09-06-40",
    )
    tail.write_text(
        "OpenAI Codex v0.137.0\n"
        "--------\n"
        "workdir: /Users/simonrowland/Repos/goal-flight\n"
        "model: gpt-5.5\n"
        "provider: openai\n"
        "approval: never\n"
        "sandbox: workspace-write [workdir, /tmp, $TMPDIR]\n"
        "reasoning effort: xhigh\n"
        "reasoning summaries: none\n"
        "session id: 01a09067-25ec-7250-9872-3a277abe5716\n"
        "--------\n"
        "user\n",
        encoding="utf-8",
    )
    watcher_argv = list(watcher["argv"][1:])
    watcher_argv[watcher_argv.index("--pid") + 1] = "99999999"
    identity_index = watcher_argv.index("--worker-identity-json")
    del watcher_argv[identity_index : identity_index + 2]
    monkeypatch.setattr(sys, "argv", watcher_argv)

    assert W.main() != 0
    harvested = json.loads(L.record_path(dispatch_id).read_text(encoding="utf-8"))
    harvested_status = json.loads(status_path.read_text(encoding="utf-8"))
    assert harvested["codex_session_id"] == CANONICAL_SESSION_ID
    assert harvested["codex_home"] == str(home)
    assert harvested["effective_account"] == account
    assert harvested_status["codex_session_id"] == CANONICAL_SESSION_ID
    assert harvested_status["codex_home"] == str(home)
    assert harvested_status["effective_account"] == account
    _, accepted_home, session_id, _ = D._validate_codex_resume_source(dispatch_id)
    assert accepted_home == home.resolve()
    assert session_id == CANONICAL_SESSION_ID


def test_canonical_resume_home_is_exactly_bound_to_recorded_account(
    tmp_path: Path,
) -> None:
    dispatch_id = "codex-48933-1789132000"
    account = "4c9435"
    home = _canonical_codex_home(tmp_path, account)
    _write_measured_rollout(
        home,
        OTHER_CANONICAL_SESSION_ID,
        "2026-09-11T09-06-40",
    )
    record = _write_parent_record(
        tmp_path,
        dispatch_id=dispatch_id,
        session_id=OTHER_CANONICAL_SESSION_ID,
        home=home,
    )
    record["effective_account"] = account
    L.write_record(record)

    _, accepted_home, session_id, _ = D._validate_codex_resume_source(dispatch_id)
    assert accepted_home == home.resolve()
    assert session_id == OTHER_CANONICAL_SESSION_ID

    rejected_homes = [
        _canonical_codex_home(tmp_path, "d78343"),
        tmp_path / "arbitrary" / "codex",
        home / ".." / "d78343" / "codex",
    ]
    for rejected in rejected_homes:
        record["codex_home"] = str(rejected)
        L.write_record(record)
        with pytest.raises(D.DispatchUsageError, match="invalid recorded codex home"):
            D._validate_codex_resume_source(dispatch_id)


def test_canonical_home_resume_uses_shared_source_without_rebuilding_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent_id = "codex-48933-1789132000"
    child_id = "canonical-home-resume-child"
    account = "4c9435"
    home = _canonical_codex_home(tmp_path, account)
    _write_measured_rollout(
        home,
        OTHER_CANONICAL_SESSION_ID,
        "2026-09-11T09-06-40",
    )
    record = _write_parent_record(
        tmp_path,
        dispatch_id=parent_id,
        session_id=OTHER_CANONICAL_SESSION_ID,
        home=home,
    )
    record["effective_account"] = account
    L.write_record(record)
    prompt = tmp_path / "resume-canonical-home.md"
    prompt.write_text("Continue this exact session.\n", encoding="utf-8")
    spawn_calls, _leases = _stub_detached_runtime(monkeypatch)
    monkeypatch.setattr(
        D,
        "_rebuild_codex_resume_home",
        lambda *_args, **_kwargs: pytest.fail(
            "a shared canonical home must not be renamed or rebuilt"
        ),
    )

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
            str(tmp_path / f"{child_id}.tail"),
            "--status-json",
            str(tmp_path / f"{child_id}.status.json"),
            "--parent-dispatch-id",
            parent_id,
            "--codex-session-id",
            OTHER_CANONICAL_SESSION_ID,
            "--codex-resume-home",
            str(home),
            "--codex-home-owner-dispatch-id",
            parent_id,
            "--launch-detached",
        ]
    )

    assert rc == 0
    worker = next(call for call in spawn_calls if call["label"] == "worker")
    assert worker["env"]["CODEX_HOME"] == str(home)
    child = json.loads(L.record_path(child_id).read_text(encoding="utf-8"))
    assert child["codex_home"] == str(home)
    assert child["effective_account"] == account
    assert child["codex_session_id"] == OTHER_CANONICAL_SESSION_ID


def _tree_snapshot(root: Path) -> dict[str, tuple[int, bytes | None]]:
    """Every path under ``root`` mapped to (mode, bytes); directories carry None."""
    snapshot: dict[str, tuple[int, bytes | None]] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        mode = path.lstat().st_mode
        snapshot[rel] = (mode, None if path.is_dir() else path.read_bytes())
    return snapshot


def _canonical_resume_argv(
    tmp_path: Path,
    *,
    parent_id: str,
    child_id: str,
    home: Path,
    prompt: Path,
    session_id: str,
    account: str | None = None,
) -> list[str]:
    argv = [
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
        str(tmp_path / f"{child_id}.tail"),
        "--status-json",
        str(tmp_path / f"{child_id}.status.json"),
        "--parent-dispatch-id",
        parent_id,
        "--codex-session-id",
        session_id,
        "--codex-resume-home",
        str(home),
        "--codex-home-owner-dispatch-id",
        parent_id,
        "--launch-detached",
    ]
    if account is not None:
        argv += ["--account", account]
    return argv


def _write_canonical_parent(
    tmp_path: Path,
    *,
    parent_id: str,
    account: str,
    with_rollout: bool = True,
) -> tuple[Path, Path | None]:
    home = _canonical_codex_home(tmp_path, account)
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(f"{account}-login", encoding="utf-8")
    rollout = None
    if with_rollout:
        rollout = _write_measured_rollout(
            home, OTHER_CANONICAL_SESSION_ID, "2026-09-11T09-06-40"
        )
    # Another session this account ran. It belongs to the account, not to the
    # resumed dispatch, and must not follow the resume anywhere.
    _write_measured_rollout(home, CANONICAL_SESSION_ID, "2026-09-10T08-00-00")
    record = _write_parent_record(
        tmp_path,
        dispatch_id=parent_id,
        session_id=OTHER_CANONICAL_SESSION_ID,
        home=home,
    )
    record["effective_account"] = account
    L.write_record(record)
    return home, rollout


def _configure_account(tmp_path: Path, account: str) -> Path:
    """Give an account its canonical home, as a configured account has."""
    home = _canonical_codex_home(tmp_path, account)
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(f"{account}-login", encoding="utf-8")
    return home


def test_cross_account_resume_copies_rollout_out_of_canonical_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A walled account's canonical-home session resumes on another account.

    A canonical account home is shared state -- that account's login and every
    session it has run -- so the resume must COPY the one rollout into a fresh
    home for the new account and leave the source byte-identical. Real case:
    codex-31568-1790103580 walled on d78343 after 21 commits and its resume on
    cf9f50 was refused outright.
    """
    parent_id = "codex-31568-1790103580"
    child_id = "cross-account-canonical-child"
    source, rollout = _write_canonical_parent(
        tmp_path, parent_id=parent_id, account="d78343"
    )
    assert rollout is not None
    target_account_home = _configure_account(tmp_path, "cf9f50")
    target_account_before = _tree_snapshot(target_account_home)
    before = _tree_snapshot(source)
    prompt = tmp_path / "resume.md"
    prompt.write_text("Continue this exact session.\n", encoding="utf-8")
    spawn_calls, _leases = _stub_detached_runtime(monkeypatch)
    target = _dispatch_home(tmp_path, child_id)
    resolve_calls: list[tuple[str | None, str]] = []

    def resolve(
        _project_root: Path, explicit_account: str | None, dispatch_id: str
    ) -> tuple[str, str]:
        resolve_calls.append((explicit_account, dispatch_id))
        target.mkdir(parents=True)
        (target / "auth.json").write_text(f"{explicit_account}-login", encoding="utf-8")
        return str(target), str(explicit_account)

    monkeypatch.setattr(D, "resolve_codex_home", resolve)
    monkeypatch.setattr(
        D,
        "_rebuild_codex_resume_home",
        lambda *_a, **_k: pytest.fail(
            "a canonical home must never be renamed, rebuilt, or deleted"
        ),
    )

    rc = D.main(
        _canonical_resume_argv(
            tmp_path,
            parent_id=parent_id,
            child_id=child_id,
            home=source,
            prompt=prompt,
            session_id=OTHER_CANONICAL_SESSION_ID,
            account="cf9f50",
        )
    )

    assert rc == 0
    # The new account's home was built for THIS dispatch, pinned to cf9f50.
    assert resolve_calls == [("cf9f50", child_id)]
    # The source account's canonical home is untouched, bytes and modes alike.
    assert _tree_snapshot(source) == before
    worker = next(call for call in spawn_calls if call["label"] == "worker")
    assert worker["env"]["CODEX_HOME"] == str(target)
    copied = S.rollout_path(target, OTHER_CANONICAL_SESSION_ID)
    assert copied is not None
    assert copied.read_bytes() == rollout.read_bytes()
    # Only the resumed session travels; the account's other sessions stay put.
    assert S.rollout_path(target, CANONICAL_SESSION_ID) is None
    child = json.loads(L.record_path(child_id).read_text(encoding="utf-8"))
    assert child["codex_home"] == str(target)
    assert child["effective_account"] == "cf9f50"
    assert child["codex_session_id"] == OTHER_CANONICAL_SESSION_ID
    # The child owns its new home, so the resumed dispatch is itself resumable.
    assert child["codex_home_owner_dispatch_id"] == child_id
    resumable_home, owner = D._codex_resume_home(child, child_id)
    assert resumable_home == target.resolve()
    assert owner == child_id
    # The new account's own canonical home is not written either.
    assert _tree_snapshot(target_account_home) == target_account_before


def test_same_account_canonical_resume_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Naming the parent's own account keeps the shared-source path exactly."""
    parent_id = "codex-48933-1789132000"
    child_id = "same-account-canonical-child"
    source, _rollout = _write_canonical_parent(
        tmp_path, parent_id=parent_id, account="4c9435"
    )
    before = _tree_snapshot(source)
    prompt = tmp_path / "resume.md"
    prompt.write_text("Continue this exact session.\n", encoding="utf-8")
    spawn_calls, _leases = _stub_detached_runtime(monkeypatch)
    monkeypatch.setattr(
        D,
        "resolve_codex_home",
        lambda *_a, **_k: pytest.fail("same-account resume must not build a home"),
    )
    monkeypatch.setattr(
        D,
        "_rebuild_codex_resume_home",
        lambda *_a, **_k: pytest.fail("a canonical home must not be rebuilt"),
    )

    rc = D.main(
        _canonical_resume_argv(
            tmp_path,
            parent_id=parent_id,
            child_id=child_id,
            home=source,
            prompt=prompt,
            session_id=OTHER_CANONICAL_SESSION_ID,
            account="4c9435",
        )
    )

    assert rc == 0
    worker = next(call for call in spawn_calls if call["label"] == "worker")
    assert worker["env"]["CODEX_HOME"] == str(source)
    child = json.loads(L.record_path(child_id).read_text(encoding="utf-8"))
    assert child["codex_home"] == str(source)
    assert child["effective_account"] == "4c9435"
    assert child["codex_home_owner_dispatch_id"] == parent_id
    assert _tree_snapshot(source) == before
    assert not _dispatch_home(tmp_path, child_id).exists()


def test_cross_account_canonical_resume_without_rollout_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No rollout, no resume: refuse before building anything for the new account."""
    parent_id = "codex-31568-1790103580"
    child_id = "cross-account-no-rollout-child"
    source, _rollout = _write_canonical_parent(
        tmp_path, parent_id=parent_id, account="d78343", with_rollout=False
    )
    _configure_account(tmp_path, "cf9f50")
    before = _tree_snapshot(source)
    prompt = tmp_path / "resume.md"
    prompt.write_text("Continue this exact session.\n", encoding="utf-8")
    _stub_detached_runtime(monkeypatch)
    monkeypatch.setattr(
        D,
        "resolve_codex_home",
        lambda *_a, **_k: pytest.fail(
            "no home may be built for the new account before the rollout is proven"
        ),
    )

    rc = D.main(
        _canonical_resume_argv(
            tmp_path,
            parent_id=parent_id,
            child_id=child_id,
            home=source,
            prompt=prompt,
            session_id=OTHER_CANONICAL_SESSION_ID,
            account="cf9f50",
        )
    )

    assert rc == 64
    error = capsys.readouterr().err
    assert "rollout missing" in error
    assert OTHER_CANONICAL_SESSION_ID in error
    assert "Traceback" not in error
    assert _tree_snapshot(source) == before
    assert not _dispatch_home(tmp_path, child_id).exists()


def test_launch_without_recordable_codex_home_warns_not_resumable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dispatch_id = "codex-no-recordable-home"
    prompt = tmp_path / "no-home.md"
    prompt.write_text("Do isolated work.\n", encoding="utf-8")
    _stub_detached_runtime(monkeypatch)
    monkeypatch.setattr(D, "_codex_seat_api", lambda: None)

    rc = D.main(
        [
            "--agent",
            "codex",
            "--unregistered-forced",
            "--shape",
            "bash",
            "--dispatch-id",
            dispatch_id,
            "--cwd",
            str(tmp_path),
            "--prompt-file",
            str(prompt),
            "--tail",
            str(tmp_path / f"{dispatch_id}.tail"),
            "--status-json",
            str(tmp_path / f"{dispatch_id}.status.json"),
            "--launch-detached",
        ]
    )

    assert rc == 0
    assert (
        f"goalflight_dispatch: dispatch {dispatch_id} will not be resumable: "
        "no recordable codex home\n"
    ) in capsys.readouterr().err


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

    # "account", not "seat": an account is a billing identity that runs many
    # concurrent sessions, so calling it a seat implies a capacity of one and
    # has repeatedly misled operators about real headroom. "seat" is reserved
    # for a worktree slot.
    assert str(exc_info.value) == (
        "could not rebuild dispatch home for restore-parent with a healthy codex account"
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


def test_closed_occupancy_fd_does_not_block_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A leftover closed occupancy fd is ended state, not occupancy unknown."""
    parent_id = "closed-fd-parent"
    home = _dispatch_home(tmp_path, parent_id)
    _write_rollout(home)
    _write_parent_record(tmp_path, dispatch_id=parent_id, home=home)
    prompt = tmp_path / "revisions.md"
    prompt.write_text("Retry after a closed occupancy fd.", encoding="utf-8")
    _stub_detached_runtime(monkeypatch)
    monkeypatch.setattr(
        D,
        "_reserve_auto_dispatch_id",
        lambda _agent, _base: "closed-fd-child",
    )
    # A closed descriptor number, not a live flock holder.
    monkeypatch.setenv(WP.OCCUPANCY_LOCK_FD_ENV, "999999")

    assert D._cmd_resume(
        [parent_id, "--prompt-file", str(prompt), "--unregistered-forced"]
    ) == 0
    retry = json.loads(L.record_path("closed-fd-child").read_text(encoding="utf-8"))
    assert retry["state"] == "running"


def test_live_occupancy_holder_still_blocks_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A genuinely live occupancy lock still refuses a second writer."""
    parent_id = "live-occ-parent"
    home = _dispatch_home(tmp_path, parent_id)
    _write_rollout(home)
    _write_parent_record(tmp_path, dispatch_id=parent_id, home=home)
    prompt = tmp_path / "revisions.md"
    prompt.write_text("Do not share this tree.", encoding="utf-8")
    _stub_detached_runtime(monkeypatch)
    monkeypatch.setattr(
        D,
        "_reserve_auto_dispatch_id",
        lambda _agent, _base: "live-occ-child",
    )
    lock = WP.try_acquire_worktree_path_lock(tmp_path, "live-holder")
    try:
        rc = D._cmd_resume(
            [parent_id, "--prompt-file", str(prompt), "--unregistered-forced"]
        )
    finally:
        lock.release()

    assert rc == 64
    err = capsys.readouterr().err
    assert "already owned" in err
    assert not L.record_path("live-occ-child").exists()


def test_capacity_refused_resume_does_not_bind_recorded_seat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Capacity refusal precedes exact-seat reattachment and holder rewrite."""
    for args in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "goalflight-test@example.invalid"],
        ["git", "config", "user.name", "Goal Flight Test"],
    ):
        result = subprocess.run(
            args,
            cwd=tmp_path,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert result.returncode == 0, (args, result.stderr)
    (tmp_path / "tracked.txt").write_text("base\n", encoding="utf-8")
    for args in (["git", "add", "tracked.txt"], ["git", "commit", "-m", "base"]):
        result = subprocess.run(
            args,
            cwd=tmp_path,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert result.returncode == 0, (args, result.stderr)

    parent_id = "capacity-seat-parent"
    child_id = "capacity-seat-child"
    home = _dispatch_home(tmp_path, parent_id)
    _write_rollout(home)
    seat_lease = WP.acquire_worktree_seat(tmp_path, parent_id, base="HEAD")
    seat = seat_lease.path
    seat_lease.release()
    record = _write_parent_record(tmp_path, dispatch_id=parent_id, home=home)
    record.update(
        {
            "project_root": str(tmp_path),
            "worker_cwd": str(seat),
            "dispatch_argv": [
                "--agent",
                "codex",
                "--shape",
                "bash",
                "--cwd",
                str(seat),
                "--worktree",
                "HEAD",
                "--prompt-file",
                str(tmp_path / "old.md"),
            ],
        }
    )
    L.write_record(record)
    prompt = tmp_path / "revisions.md"
    prompt.write_text("Resume only after capacity admission.\n", encoding="utf-8")
    _stub_detached_runtime(monkeypatch)

    def deny_capacity(args, *, project_root, status_json):
        D.write_status(
            status_json,
            {
                "state": "blocked_capacity",
                "reason": {"reason": "machine_worker_cap"},
            },
        )
        raise SystemExit(2)

    monkeypatch.setattr(D, "_acquire_capacity", deny_capacity)
    bind_calls: list[str] = []
    original_record = D._record_dispatch_worktree

    def record_bind(args, lease):
        bind_calls.append(str(lease.path))
        return original_record(args, lease)

    monkeypatch.setattr(D, "_record_dispatch_worktree", record_bind)
    monkeypatch.setattr(D, "_reserve_auto_dispatch_id", lambda *_args: child_id)

    with pytest.raises(SystemExit) as exc_info:
        D._cmd_resume(
            [parent_id, "--prompt-file", str(prompt), "--unregistered-forced"]
        )
    assert exc_info.value.code == 2
    assert bind_calls == []
    assert json.loads(L.record_path(child_id).read_text(encoding="utf-8"))["state"] == "blocked_capacity"
    assert not WP.worktree_seat_lock_path(tmp_path, seat.name).exists()


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


def test_resume_refuses_worker_dead_source_whose_pid_is_live(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CONTROL: a dead label with a live pid is still the two-writer case.

    The lineage exemption must not become a way past the liveness probe.
    """
    parent_id = "dead-but-live"
    home = _dispatch_home(tmp_path, parent_id)
    _write_rollout(home)
    record = _write_parent_record(
        tmp_path,
        dispatch_id=parent_id,
        home=home,
    )
    record.update(
        {
            "state": "worker_dead",
            "terminal_state": "worker_dead",
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
    monkeypatch.setattr(L, "identity_matches", lambda _record: (True, "live"))
    monkeypatch.setattr(
        D,
        "_reserve_auto_dispatch_id",
        lambda *_args, **_kwargs: pytest.fail(
            "a live pid must not allocate a child dispatch"
        ),
    )

    rc = D.main(["resume", parent_id, "--prompt-file", str(prompt)])

    assert rc == 64
    assert capsys.readouterr().err == (
        "goalflight_dispatch: dispatch dead-but-live is still live; "
        "wait for terminal before resume\n"
    )


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


def _option_value(argv: list[str], flag: str) -> str | None:
    prefix = flag + "="
    for index, token in enumerate(argv):
        if token == "--":
            break
        if token == flag:
            if index + 1 >= len(argv):
                return None
            return argv[index + 1]
        if token.startswith(prefix):
            return token[len(prefix) :]
    return None


def test_resume_reuses_worktree_and_does_not_mint_a_new_seat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Failing-before / passing-after: resume keeps the parent tree.

    A parent launched with ``--worktree HEAD`` used to reconstruct that flag.
    The child then acquired a fresh pooled seat, reset it, and abandoned the
    partial work. Resume argv must pin the recorded cwd and leave exact-seat
    locking to the reconstructed dispatch path.
    """
    parent_id = "wt-resume-parent"
    worktree = tmp_path / "worktrees" / "controller" / "s-1"
    worktree.mkdir(parents=True)
    (worktree / "partial.txt").write_text("keep me\n", encoding="utf-8")
    home = _dispatch_home(tmp_path, parent_id)
    _write_rollout(home)
    record = _write_parent_record(tmp_path, dispatch_id=parent_id, home=home)
    record.update(
        {
            "worker_cwd": str(worktree),
            "state": "quota_exhausted",
            "terminal_state": "quota_exhausted",
            "dispatch_argv": [
                "--agent",
                "codex",
                "--shape",
                "bash",
                "--dispatch-id",
                parent_id,
                "--cwd",
                str(worktree),
                "--worktree",
                "HEAD",
                "--prompt-file",
                str(tmp_path / "old.md"),
            ],
        }
    )
    L.write_record(record)
    prompt = tmp_path / "revisions.md"
    prompt.write_text("Continue in the same tree.\n", encoding="utf-8")
    acquire_calls: list[tuple] = []

    def refuse_acquire(*args, **kwargs):
        acquire_calls.append((args, kwargs))
        raise AssertionError("resume must not mint a sibling worktree")

    monkeypatch.setattr(WP, "acquire_worktree_seat", refuse_acquire)
    captured: list[list[str]] = []
    monkeypatch.setattr(
        D,
        "_reserve_auto_dispatch_id",
        lambda _agent, _base: "wt-resume-child",
    )
    monkeypatch.setattr(
        D,
        "main",
        lambda argv=None: captured.append(list(argv or [])) or 0,
    )

    assert (
        D._cmd_resume(
            [parent_id, "--prompt-file", str(prompt), "--unregistered-forced"]
        )
        == 0
    )
    launch = captured[0]
    assert "--worktree" not in launch
    assert Path(_option_value(launch, "--cwd") or "").resolve() == worktree.resolve()
    assert acquire_calls == []
    assert (worktree / "partial.txt").read_text(encoding="utf-8") == "keep me\n"


def test_bind_dispatch_worktree_reacquires_recorded_seat_without_reset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seat = tmp_path / "worktrees" / "controller" / "s-1"
    lease = SimpleNamespace(path=seat)
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def acquire(*args, **kwargs):
        calls.append((args, kwargs))
        return lease

    monkeypatch.setattr(
        WP,
        "acquire_worktree_seat",
        acquire,
    )
    monkeypatch.setattr(D, "_project_root", lambda _args: tmp_path)
    monkeypatch.setattr(D, "_controller_ring_label", lambda *_args: "controller")
    monkeypatch.setattr(WP, "classify_dispatch_cwd", lambda *_args, **_kwargs: "ring-seat")
    monkeypatch.setattr(WP, "_git", lambda *_args, **_kwargs: "a" * 40)
    args = SimpleNamespace(
        worktree="HEAD",
        parent_dispatch_id="parent-dispatch",
        dispatch_id="child-dispatch",
        cwd=str(seat),
        skip_seat_reset=True,
        in_place=False,
        _worktree_seat=None,
    )
    assert D._bind_dispatch_worktree(args) is lease
    assert len(calls) == 1
    call_args, call_kwargs = calls[0]
    assert call_args == (tmp_path, "child-dispatch")
    assert call_kwargs["controller_label"] == "controller"
    assert call_kwargs["reset"] is False
    assert call_kwargs["occupy_path"] == seat.resolve()
    assert call_kwargs["expected_prior_dispatch_id"] == "parent-dispatch"
    assert args.cwd == str(seat)
    assert args._worktree_base_commit == "a" * 40


def test_resume_of_quota_exhausted_dispatch_honors_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent_id = "quota-parent"
    home = _dispatch_home(tmp_path, parent_id)
    _write_rollout(home)
    record = _write_parent_record(tmp_path, dispatch_id=parent_id, home=home)
    record.update(
        {
            "state": "quota_exhausted",
            "terminal_state": "quota_exhausted",
            "effective_account": "cf9f50",
            "account": "cf9f50",
            "reason": {
                "limit_state": "quota_exhausted",
                "reset_at": "2033-09-07T02:18:00+00:00",
            },
        }
    )
    L.write_record(record)
    prompt = tmp_path / "revisions.md"
    prompt.write_text("Continue after quota death.\n", encoding="utf-8")
    captured: list[list[str]] = []
    monkeypatch.setattr(
        D,
        "_reserve_auto_dispatch_id",
        lambda _agent, _base: "quota-resume-child",
    )
    monkeypatch.setattr(
        D,
        "main",
        lambda argv=None: captured.append(list(argv or [])) or 0,
    )

    assert (
        D._cmd_resume(
            [
                parent_id,
                "--prompt-file",
                str(prompt),
                "--unregistered-forced",
                "--account",
                "25ca6b",
            ]
        )
        == 0
    )
    launch = captured[0]
    assert _option_value(launch, "--account") == "25ca6b"
    assert launch[launch.index("--parent-dispatch-id") + 1] == parent_id
    assert Path(_option_value(launch, "--cwd") or "").resolve() == tmp_path.resolve()


def test_resume_of_plan_approval_pause_reuses_worktree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Plan-approval / !READY pause: resume is the continue, not a sibling tree."""
    parent_id = "plan-parent"
    worktree = tmp_path / "worktrees" / "b-3374"
    worktree.mkdir(parents=True)
    (worktree / "PLAN.md").write_text("# plan awaiting approval\n", encoding="utf-8")
    home = _dispatch_home(tmp_path, parent_id)
    _write_rollout(home)
    record = _write_parent_record(tmp_path, dispatch_id=parent_id, home=home)
    record.update(
        {
            "worker_cwd": str(worktree),
            "state": "awaiting_user_confirm",
            "terminal_state": "unknown",
            "classification": "stale_dead",
            "dispatch_argv": [
                "--agent",
                "codex",
                "--cwd",
                str(worktree),
                "--worktree",
                "HEAD",
                "--prompt-file",
                str(tmp_path / "plan.md"),
            ],
        }
    )
    L.write_record(record)
    prompt = tmp_path / "approve.md"
    prompt.write_text("Controller approved the PLAN. Continue.\n", encoding="utf-8")
    captured: list[list[str]] = []
    monkeypatch.setattr(
        D,
        "_reserve_auto_dispatch_id",
        lambda _agent, _base: "plan-resume-child",
    )
    monkeypatch.setattr(
        D,
        "main",
        lambda argv=None: captured.append(list(argv or [])) or 0,
    )

    assert (
        D._cmd_resume(
            [parent_id, "--prompt-file", str(prompt), "--unregistered-forced"]
        )
        == 0
    )
    launch = captured[0]
    assert "--worktree" not in launch
    assert Path(_option_value(launch, "--cwd") or "").resolve() == worktree.resolve()
    assert (worktree / "PLAN.md").is_file()


def test_resume_occupancy_skips_parent_plan_approval_row(
    tmp_path: Path,
) -> None:
    parent_id = "occ-plan-parent"
    child_id = "occ-plan-child"
    home = _dispatch_home(tmp_path, parent_id)
    _write_rollout(home)
    record = _write_parent_record(tmp_path, dispatch_id=parent_id, home=home)
    record.update(
        {
            "worker_cwd": str(tmp_path),
            "state": "awaiting_user_confirm",
            "terminal_state": "unknown",
            "hostname": __import__("socket").gethostname(),
        }
    )
    L.write_record(record)
    args = SimpleNamespace(
        cwd=str(tmp_path),
        dispatch_id=child_id,
        parent_dispatch_id=parent_id,
        read_only=False,
        os_sandbox=None,
    )
    occupied, unknown, occupied_state = D._worktree_incumbent_reason(args)
    assert occupied is None
    assert unknown is None
    assert occupied_state is None


def test_resume_rebuild_honors_explicit_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dispatch_id = "explicit-seat-parent"
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
        (home / "auth.json").write_text(str(explicit_account), encoding="utf-8")
        return str(home), str(explicit_account)

    monkeypatch.setattr(D, "resolve_codex_home", resolve)
    monkeypatch.setattr(D, "cleanup_codex_dispatch_home", lambda _dispatch_id: None)

    rebuilt, effective_account = D._rebuild_codex_resume_home(
        tmp_path,
        dispatch_id,
        home,
        SESSION_ID,
        explicit_account="25ca6b",
    )

    assert calls == [(tmp_path, "25ca6b", dispatch_id)]
    assert rebuilt == str(home)
    assert effective_account == "25ca6b"
    assert (home / "auth.json").read_text(encoding="utf-8") == "25ca6b"
    assert S.rollout_path(home, SESSION_ID) == rollout


def test_unpinned_codex_selection_skips_recently_exhausted_seat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = Path(os.environ["GOALFLIGHT_CODEX_STATE_DIR"])
    state.mkdir(parents=True, exist_ok=True)
    (state / "codex-seat-states.json").write_text(
        json.dumps(
            {
                "version": 1,
                "seats": {
                    "4c9435": {"cooldown_until": "2033-09-07T02:18:00+00:00"},
                    "25ca6b": {},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    accounts = tmp_path / "home" / ".goal-flight" / "accounts"
    for name in ("4c9435", "25ca6b"):
        (accounts / name / "codex").mkdir(parents=True)
    L.write_record(
        {
            "schema": L.SCHEMA,
            "dispatch_id": "dead-seat-row",
            "agent": "codex",
            "engine": "codex",
            "effective_account": "4c9435",
            "state": "quota_exhausted",
            "terminal_state": "quota_exhausted",
            "reset_at": "2033-09-07T02:18:00+00:00",
            "started_at": L.utc_now(),
        }
    )
    seen: list[str | None] = []

    def resolve_seat(_project_root, explicit_account, _dispatch_id):
        seen.append(explicit_account)
        if explicit_account is None:
            return str(tmp_path / "home-4c9435"), "4c9435"
        return str(tmp_path / f"home-{explicit_account}"), explicit_account

    monkeypatch.setattr(
        D,
        "_codex_seat_api",
        lambda: SimpleNamespace(resolve_codex_seat=resolve_seat),
    )
    monkeypatch.setattr(
        D,
        "_codex_usage_probe_says_usable",
        lambda account, **kwargs: account == "25ca6b",
    )

    home, account = D.resolve_codex_home(tmp_path, None, "fresh-dispatch")
    assert seen == ["25ca6b"]
    assert account == "25ca6b"
    assert home.endswith("home-25ca6b")


def test_explicit_account_is_honored_even_when_not_first(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seen: list[str | None] = []

    def resolve_seat(_project_root, explicit_account, _dispatch_id):
        seen.append(explicit_account)
        return str(tmp_path / "home-d78343"), explicit_account

    monkeypatch.setattr(
        D,
        "_codex_seat_api",
        lambda: SimpleNamespace(resolve_codex_seat=resolve_seat),
    )
    home, account = D.resolve_codex_home(tmp_path, "d78343", "pinned-dispatch")
    assert seen == ["d78343"]
    assert account == "d78343"
    assert home.endswith("home-d78343")


def test_account_quota_blocked_holds_until_reset(tmp_path: Path) -> None:
    L.write_record(
        {
            "schema": L.SCHEMA,
            "dispatch_id": "exhausted-row",
            "agent": "codex",
            "engine": "codex",
            "effective_account": "86e5d2",
            "state": "quota_exhausted",
            "terminal_state": "quota_exhausted",
            "reset_at": "2033-09-07T02:18:00+00:00",
            "started_at": L.utc_now(),
        }
    )
    assert D._account_quota_blocked("86e5d2", engine="codex") is True
    assert D._account_quota_blocked("25ca6b", engine="codex") is False
