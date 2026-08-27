#!/usr/bin/env python3
"""Regressions for t-377: a live worker reported as a failed launch.

A refused journal attempt transition after spawn is a bookkeeping problem,
not evidence about the worker. The dispatcher must still write the status
file (the liveness authority), must still run the status export / registry
upsert, and must surface the refusal loudly — while the retryable startup
race (attempt_not_yet_running) is re-recorded against a bounded deadline
instead of failing anything at all.

Every refusal here is induced against a REAL journal via the REAL
cmd_record: no test stubs the exit code under test.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
os.environ["GOALFLIGHT_ACP_PYTHON"] = str(ROOT / ".missing-acp-test-python")

import goalflight_dispatch as D  # noqa: E402
import goalflight_ledger as L  # noqa: E402


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="spawn/liveness assertions are POSIX-only",
)


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_DIR", str(tmp_path / "dispatch"))
    monkeypatch.setenv("GOALFLIGHT_JOURNAL_DIR", str(tmp_path / "journals"))
    monkeypatch.setenv("GOALFLIGHT_MESSAGES_DIR", str(tmp_path / "messages"))
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE_DIR", str(tmp_path / "task-store"))
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE", str(tmp_path / "task-store"))
    monkeypatch.setenv("GOALFLIGHT_WAKE_LEDGER_DIR", str(tmp_path / "wake-ledger"))
    monkeypatch.setenv("GOALFLIGHT_WAKE_LEDGER", str(tmp_path / "wake-ledger"))
    monkeypatch.setenv("GOALFLIGHT_PIDFILE_DIR", str(tmp_path / "pids"))
    monkeypatch.setenv("GOAL_FLIGHT_PIDFILE_DIR", str(tmp_path / "pids"))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", "/dev/null")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_WAIT_S", "0")
    monkeypatch.setenv("GOALFLIGHT_DISABLE_NUDGES", "1")
    for key in (
        "GOALFLIGHT_CONTROLLER_LABEL",
        "GOALFLIGHT_CONTROLLER_PID",
        "GOALFLIGHT_CONTROLLER_SESSION_ID",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def export_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the visibility side effects a refused launch used to skip."""
    calls: list[str] = []
    monkeypatch.setattr(
        D,
        "_export_dashboard_status_for_project",
        lambda *_a, **_k: calls.append("export"),
    )
    monkeypatch.setattr(
        D,
        "_upsert_project_registry_for_dispatch",
        lambda *_a, **_k: calls.append("registry"),
    )
    monkeypatch.setattr(
        D,
        "_start_dashboard_refresh_for_project",
        lambda *_a, **_k: calls.append("refresh"),
    )
    return calls


@pytest.fixture
def spawned_worker():
    """A real spawned child process standing in for the worker."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait()


def _dispatch_args(project: Path, dispatch_id: str) -> argparse.Namespace:
    return argparse.Namespace(
        dispatch_id=dispatch_id,
        task_ids=[],
        agent="codex",
        shape="bash",
        account="default",
        read_only=False,
        os_sandbox=None,
        controller_pid=None,
        controller_session_id=None,
        controller_label=None,
        queue_launch_token=None,
        launch_detached=False,
        cwd=str(project),
        priority="normal",
        billing="sub",
        poll_secs=2.0,
        max_idle_secs=0,
        prompt_file=None,
        prompt=None,
        model=None,
        web_research_ok=False,
        ignore_git_warn=False,
        capacity_wait_s=None,
        interactive=False,
        permission_mode=None,
        permission_dir=None,
        permission_inline_timeout_s=None,
        permission_user_timeout_s=None,
    )


def _record(
    args: argparse.Namespace,
    project: Path,
    tmp_path: Path,
    *,
    worker_pid,
    state: str,
) -> dict | None:
    return D._record_ledger(
        args,
        project_root=project,
        prompt_path=None,
        status_json=tmp_path / f"{args.dispatch_id}.status.json",
        tail=tmp_path / f"{args.dispatch_id}.tail",
        lease_id=None,
        worker_pid=worker_pid,
        state=state,
    )


def _prepare_starting_attempt(
    args: argparse.Namespace, project: Path, tmp_path: Path
) -> None:
    """Drive the REAL pre-spawn record path: attempt PREPARED then STARTING."""
    _record(args, project, tmp_path, worker_pid=None, state="starting")


def _status_payload(tmp_path: Path, dispatch_id: str) -> dict:
    return json.loads(
        (tmp_path / f"{dispatch_id}.status.json").read_text(encoding="utf-8")
    )


def _ledger_record(dispatch_id: str) -> dict:
    return json.loads(L.record_path(dispatch_id).read_text(encoding="utf-8"))


def test_worker_spawn_state_none_is_not_unknown() -> None:
    """None is the only definitive "no worker"; anything else is not "none".

    Spawn helpers return a positive pid or raise, so None means this path did
    not spawn. A non-int, non-positive, or bool pid cannot prove absence.
    """
    assert L.worker_spawn_state(None) == "none"
    assert L.worker_spawn_state(1) == "spawned"
    assert L.worker_spawn_state(os.getpid()) == "spawned"
    assert L.worker_spawn_state("not-a-pid") == "unknown"
    assert L.worker_spawn_state(0) == "unknown"
    assert L.worker_spawn_state(-1) == "unknown"
    assert L.worker_spawn_state(True) == "unknown"


def test_running_refusal_payload_is_structured_and_retryable(
    tmp_path: Path,
) -> None:
    """The ledger reports the startup race honestly: attempt_not_yet_running.

    A fabricated "cas_lost" disposition here is what made the dispatcher
    treat an in-flight claim as a lost compare-and-swap and fail the launch.
    """
    project = tmp_path / "project"
    project.mkdir()
    args = _dispatch_args(project, "t377-payload")
    _prepare_starting_attempt(args, project, tmp_path)

    capture = argparse.Namespace(
        dispatch_id=args.dispatch_id,
        prompt_id=None,
        prompt_path=None,
        task_ids=[],
        agent="codex",
        engine="codex",
        shape="bash",
        account="default",
        effective_account=None,
        transport="dispatch",
        project_root=str(project),
        controller_pid=None,
        worker_pid=None,
        acp_session_id=None,
        logical_session_id=args.dispatch_id,
        lease_id=None,
        stdout_path=None,
        stderr_path=None,
        status_path=None,
        os_sandbox_json=None,
        queue_launch_token=None,
        detached=False,
        state="running",
        json=True,
    )
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = L.cmd_record(capture)
    assert code == 3
    refusal = L.parse_record_refusal(buf.getvalue())
    assert refusal is not None
    assert refusal["disposition"] == "attempt_not_yet_running"
    assert refusal["retryable"] is True
    assert L.is_retryable_startup_race(refusal)


def test_spawned_worker_genuine_refusal_writes_status_and_warns(
    tmp_path: Path,
    export_calls: list[str],
    spawned_worker: subprocess.Popen,
    capsys: pytest.CaptureFixture,
) -> None:
    """Spawned worker + genuine non-retryable refusal: warn, don't fail.

    A mismatched launch token makes prepare_attempt lose a REAL CAS, which is
    not retryable. The dispatch must not be reported failed: the status file
    exists, names the refusal, and the export/upsert still run.
    """
    project = tmp_path / "project"
    project.mkdir()
    args = _dispatch_args(project, "t377-genuine-refusal")
    _prepare_starting_attempt(args, project, tmp_path)
    export_calls.clear()
    capsys.readouterr()

    args.queue_launch_token = "bogus-token"  # real CAS loss on the real journal
    started = time.monotonic()
    warning = _record(args, project, tmp_path, worker_pid=spawned_worker.pid, state="running")
    elapsed = time.monotonic() - started

    assert warning is not None
    assert warning["disposition"] == "cas_lost"
    assert warning["retryable"] is None
    assert warning["state"] == "running"
    assert elapsed < L.RECORD_STARTUP_RACE_RETRY_BUDGET_S  # no race: no retry burn

    status = _status_payload(tmp_path, args.dispatch_id)
    assert status["state"] == "running"  # the worker, not the bookkeeping
    assert status["worker_pid"] == spawned_worker.pid
    assert status["worker_alive"] is True
    assert status["ledger_record_warning"]["disposition"] == "cas_lost"
    assert "do not blind-retry" in status["ledger_record_warning"]["detail"]

    err = capsys.readouterr().err
    assert "DISPATCH-LEDGER-WARN" in err
    assert "cas_lost" in err
    assert args.dispatch_id in err

    assert "export" in export_calls
    assert "registry" in export_calls


def test_startup_race_is_retried_and_commits_once_running(
    tmp_path: Path,
    export_calls: list[str],
    spawned_worker: subprocess.Popen,
    capsys: pytest.CaptureFixture,
) -> None:
    """The startup race resolves itself: re-record commits, no warning.

    First record attempt genuinely refuses (attempt still STARTING); the real
    claim lands 0.4s later on a background thread, exactly as the worker's
    asynchronous RUNNING claim does in production.
    """
    project = tmp_path / "project"
    project.mkdir()
    args = _dispatch_args(project, "t377-race-retry")
    _prepare_starting_attempt(args, project, tmp_path)
    export_calls.clear()
    capsys.readouterr()

    claim_delay_s = 0.4

    def _claim_later() -> None:
        time.sleep(claim_delay_s)
        L.claim_attempt_running(project, args.dispatch_id, spawned_worker.pid)

    threading.Thread(target=_claim_later, daemon=True).start()

    started = time.monotonic()
    warning = _record(args, project, tmp_path, worker_pid=spawned_worker.pid, state="running")
    elapsed = time.monotonic() - started

    assert warning is None  # retried record committed: nothing to warn about
    assert elapsed >= claim_delay_s * 0.75  # proves the first attempt refused
    record = _ledger_record(args.dispatch_id)
    assert record["state"] == "running"
    assert record["worker_pid"] == spawned_worker.pid
    assert record["attempt_id"]

    err = capsys.readouterr().err
    assert "DISPATCH-LEDGER-WARN" not in err

    assert "export" in export_calls
    assert "registry" in export_calls


def test_startup_race_budget_exhausted_still_warns_and_writes_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    export_calls: list[str],
    spawned_worker: subprocess.Popen,
    capsys: pytest.CaptureFixture,
) -> None:
    """Race that never resolves inside the deadline: warn, don't fail.

    The retry is bounded (budget patched down so the test does not burn the
    production 10s); when the deadline expires with a spawned worker the
    status file must still be written and the refusal still named.
    """
    monkeypatch.setattr(L, "RECORD_STARTUP_RACE_RETRY_BUDGET_S", 0.6)
    project = tmp_path / "project"
    project.mkdir()
    args = _dispatch_args(project, "t377-race-exhausted")
    _prepare_starting_attempt(args, project, tmp_path)
    export_calls.clear()
    capsys.readouterr()

    started = time.monotonic()
    warning = _record(args, project, tmp_path, worker_pid=spawned_worker.pid, state="running")
    elapsed = time.monotonic() - started

    assert warning is not None
    assert warning["disposition"] == "attempt_not_yet_running"
    assert warning["retryable"] is True
    assert 0.5 <= elapsed < 5.0  # bounded deadline, not the production budget

    status = _status_payload(tmp_path, args.dispatch_id)
    assert status["state"] == "running"
    assert status["worker_alive"] is True
    assert (
        status["ledger_record_warning"]["disposition"] == "attempt_not_yet_running"
    )

    err = capsys.readouterr().err
    assert "DISPATCH-LEDGER-WARN" in err
    assert "attempt_not_yet_running" in err

    assert "export" in export_calls
    assert "registry" in export_calls


def test_no_worker_spawned_refusal_still_raises(
    tmp_path: Path,
    export_calls: list[str],
    capsys: pytest.CaptureFixture,
) -> None:
    """No worker + refusal: unchanged behaviour — raise, no status file."""
    project = tmp_path / "project"
    project.mkdir()
    args = _dispatch_args(project, "t377-no-worker")
    _prepare_starting_attempt(args, project, tmp_path)
    export_calls.clear()
    capsys.readouterr()

    args.queue_launch_token = "bogus-token"
    with pytest.raises(RuntimeError, match="journal attempt transition refused"):
        _record(args, project, tmp_path, worker_pid=None, state="running")

    assert not (tmp_path / f"{args.dispatch_id}.status.json").exists()
    assert export_calls == []
    assert "DISPATCH-LEDGER-WARN" not in capsys.readouterr().err


def test_indeterminate_spawn_state_takes_the_safe_branch(
    tmp_path: Path,
    export_calls: list[str],
    capsys: pytest.CaptureFixture,
) -> None:
    """"Could not determine whether a worker was spawned" is not "no worker".

    A garbage worker_pid cannot prove no process exists, so the refusal takes
    the spawned branch: status file written, warning emitted, no raise.
    """
    project = tmp_path / "project"
    project.mkdir()
    args = _dispatch_args(project, "t377-indeterminate")
    _prepare_starting_attempt(args, project, tmp_path)
    export_calls.clear()
    capsys.readouterr()

    args.queue_launch_token = "bogus-token"
    warning = _record(args, project, tmp_path, worker_pid="not-a-pid", state="running")

    assert warning is not None
    status = _status_payload(tmp_path, args.dispatch_id)
    assert status["ledger_record_warning"]["disposition"] == "cas_lost"
    assert status["worker_alive"] is None  # honestly unknown
    assert "DISPATCH-LEDGER-WARN" in capsys.readouterr().err
    assert "export" in export_calls
    assert "registry" in export_calls
