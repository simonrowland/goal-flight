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
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
# Import path only. Do not mutate process env at module scope: collection
# imports every selected file before any fixture runs, so an os.environ write
# here leaks into later modules in the same pytest process.
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_acp_run as A  # noqa: E402
import goalflight_dispatch as D  # noqa: E402
import goalflight_journal as J  # noqa: E402
import goalflight_ledger as L  # noqa: E402
import goalflight_review_job as R  # noqa: E402


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
    monkeypatch.setenv(
        "GOALFLIGHT_ACP_PYTHON", str(ROOT / ".missing-acp-test-python")
    )
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


def _acp_cfg(dispatch_id: str) -> argparse.Namespace:
    return argparse.Namespace(
        prompt_id=None,
        prompt=None,
        task_ids=[],
        agent="codex",
        account="default",
        request_envelope=None,
        session_id=dispatch_id,
        engine_session_id=None,
        queue_launch_token=None,
    )


def _record_acp(
    cfg: argparse.Namespace,
    project: Path,
    tmp_path: Path,
    *,
    dispatch_id: str,
    worker_pid,
    state: str,
    payload: dict | None = None,
) -> dict | None:
    body = payload if payload is not None else {"dispatch_id": dispatch_id, "state": state}
    return A._record_acp_ledger_state(
        cfg,
        dispatch_id=dispatch_id,
        project_root=project,
        controller_pid=None,
        controller_session_id=None,
        controller_label=None,
        status_path=tmp_path / f"{dispatch_id}.status.json",
        payload=body,
        effective_account=None,
        lease_id=None,
        worker_pid=worker_pid,
        state=state,
    )


def _review_args(project: Path, tmp_path: Path, name: str) -> argparse.Namespace:
    prompt = tmp_path / f"{name}.prompt.md"
    prompt.write_text("review this\n", encoding="utf-8")
    return argparse.Namespace(
        agent="codex",
        name=name,
        repo=str(project),
        prompt=str(prompt),
    )


def _record_review(
    args: argparse.Namespace,
    tmp_path: Path,
    *,
    dispatch_id: str,
    worker_pid,
    state: str,
) -> dict | None:
    return R._record_review_ledger_state(
        args,
        dispatch_id=dispatch_id,
        lease_id=None,
        stdout_path=tmp_path / f"{dispatch_id}.stdout.jsonl",
        stderr_path=tmp_path / f"{dispatch_id}.stderr.log",
        status_path=tmp_path / f"{dispatch_id}.status.json",
        state=state,
        worker_pid=worker_pid,
    )


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


def test_wait_and_prelaunch_finish_clear_inherited_worker_evidence(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    dispatch_id = "prelaunch-child-session-cleanup"
    args = _dispatch_args(project, dispatch_id)
    args.parent_dispatch_id = "parent-dispatch"
    args.engine_session_id = "inherited-engine-session"
    args.codex_session_id = "inherited-codex-session"

    _record(args, project, tmp_path, worker_pid=None, state="waiting_capacity")
    waiting = _ledger_record(dispatch_id)
    assert "engine_session_id" not in waiting
    assert "codex_session_id" not in waiting
    assert "worker_cwd" not in waiting

    waiting["engine_session_id"] = args.engine_session_id
    waiting["codex_session_id"] = args.codex_session_id
    waiting["worker_cwd"] = str(project)
    L.write_record(waiting)
    D._record_prelaunch_failure(
        args,
        error="worktree admission refused before spawn",
        allow_existing_record=True,
    )

    finished = _ledger_record(dispatch_id)
    assert finished["prelaunch_failure"] is True
    assert "engine_session_id" not in finished
    assert "codex_session_id" not in finished
    assert "worker_cwd" not in finished


@pytest.mark.parametrize("existing_record", [False, True], ids=("new", "queued"))
def test_prelaunch_refusal_stamps_owner_before_blocked_event_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    existing_record: bool,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    authority = J.open_or_create_journal(project)
    label = "prelaunch-controller"
    lease = authority.claim_or_renew_lease(
        label,
        principal={"pid": os.getpid(), "start_token": "prelaunch-controller"},
    )
    assert lease.committed and lease.value is not None
    args = _dispatch_args(project, "prelaunch-owned-refusal")

    def stamp_controller(args, root):
        assert root == project
        args.controller_label = label
        args.controller_session_id = lease.value.nonce
        args._controller_beacon_pid = os.getpid()
        return {"reason": "test-stamp"}

    monkeypatch.setattr(D, "_stamp_controller_session", stamp_controller)
    if existing_record:
        L.write_record(
            {
                "schema": L.SCHEMA,
                "dispatch_id": args.dispatch_id,
                "agent": "codex",
                "engine": "codex",
                "shape": "bash",
                "transport": "dispatch",
                "project_root": str(project),
                "worker_pid": None,
                "state": "queued",
                "terminal_state": "unknown",
            }
        )
    D._finish_permanent_prelaunch_refusal(
        args.dispatch_id,
        "seat_preparation_failed: capacity unavailable",
        state="failed",
        args=args,
    )

    record = _ledger_record(args.dispatch_id)
    assert record["controller_label"] == label
    assert record["controller_session_id"] == lease.value.nonce
    assert record["controller_pid"] == os.getpid()
    attempt = authority.read_all(
        "SELECT owner_controller_label FROM dispatch_attempts WHERE dispatch_id = ?",
        (args.dispatch_id,),
    )[0]
    assert attempt["owner_controller_label"] == label
    delivery = authority.read_all(
        "SELECT recipient_label, event_type FROM delivery_events WHERE stream_id = ?",
        (args.dispatch_id,),
    )
    assert [(row["recipient_label"], row["event_type"]) for row in delivery] == [
        (label, "blocked")
    ]


def test_reparented_prelaunch_keeps_explicit_label_without_roster_fanout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "example-sim-engine"
    project.mkdir()
    authority = J.open_or_create_journal(project)
    for label in ("example-sim-physics", "example-sim-bugs", "example-sim-train"):
        claimed = authority.claim_or_renew_lease(
            label,
            principal={"pid": os.getpid(), "start_token": label},
        )
        assert claimed.committed
    args = _dispatch_args(project, "reparented-explicit-owner")
    args.controller_label = "example-sim-engine"
    args._requested_controller_label = "example-sim-engine"

    def reparented_stamp(args, _root):
        # A nohup-reparented launcher cannot prove ancestry, but its explicit
        # launch label remains the terminal event's address.
        args.controller_label = None
        args.controller_session_id = None
        args._controller_beacon_pid = None
        return {"claimed": False, "reason": "not_in_ancestry"}

    monkeypatch.setattr(D, "_stamp_controller_session", reparented_stamp)
    D._record_prelaunch_failure(
        args,
        error="synthetic prelaunch refusal",
        reason="blocked_os_sandbox",
        state="blocked_os_sandbox",
    )

    record = _ledger_record(args.dispatch_id)
    assert record["controller_label"] == "example-sim-engine"
    delivery = authority.read_all(
        "SELECT recipient_label, event_type FROM delivery_events WHERE stream_id = ?",
        (args.dispatch_id,),
    )
    assert [(row["recipient_label"], row["event_type"]) for row in delivery] == [
        ("example-sim-engine", "blocked")
    ]
    envelope = json.loads(
        (tmp_path / "messages" / f"{args.dispatch_id}.jsonl").read_text().splitlines()[0]
    )
    assert envelope["addressee"]["label"] == "example-sim-engine"
    assert envelope["addressee"]["project_root"] == str(project.resolve())


def test_acp_prelaunch_refusal_uses_the_same_controller_stamp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    args = _dispatch_args(project, "acp-prelaunch-refusal")
    args.status_json = str(tmp_path / "acp-prelaunch-refusal.status.json")
    label = "acp-prelaunch-controller"

    def stamp_controller(args, _root):
        args.controller_label = label
        args.controller_session_id = "acp-session"
        args._controller_beacon_pid = os.getpid()
        return {"reason": "test-stamp"}

    monkeypatch.setattr(D, "_stamp_controller_session", stamp_controller)
    assert D._record_acp_prelaunch_refusal(
        args,
        error="account refused",
        reason="account_refused",
        state="failed",
    ) == 64

    record = _ledger_record(args.dispatch_id)
    assert record["controller_label"] == label
    assert record["controller_session_id"] == "acp-session"
    assert record["controller_pid"] == os.getpid()


def test_unsupported_sandbox_refusal_stamps_controller_before_recording(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    args = _dispatch_args(project, "unsupported-sandbox-refusal")
    args.status_json = str(tmp_path / "unsupported-sandbox-refusal.status.json")
    args.tail = str(tmp_path / "unsupported-sandbox-refusal.tail")

    def stamp_controller(args, _root):
        args.controller_label = "sandbox-refusal-controller"
        args.controller_session_id = "sandbox-refusal-session"
        args._controller_beacon_pid = os.getpid()
        return {"reason": "test-stamp"}

    monkeypatch.setattr(D, "_stamp_controller_session", stamp_controller)
    assert D._record_unsupported_sandbox_rejection(
        args,
        D.UnsupportedAgentSandboxRequest("inert sandbox request"),
    ) == 64

    record = _ledger_record(args.dispatch_id)
    assert record["controller_label"] == "sandbox-refusal-controller"
    assert record["controller_session_id"] == "sandbox-refusal-session"
    assert record["controller_pid"] == os.getpid()


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
    assert status["spawn_state"] == "spawned"
    assert status["ledger_record_warning"]["disposition"] == "cas_lost"
    assert status["ledger_record_warning"]["spawn_state"] == "spawned"
    assert "do not blind-retry" in status["ledger_record_warning"]["detail"]

    err = capsys.readouterr().err
    assert "DISPATCH-LEDGER-WARN" in err
    assert "cas_lost" in err
    assert args.dispatch_id in err
    warn_line = next(
        line for line in err.splitlines() if line.startswith("DISPATCH-LEDGER-WARN ")
    )
    warn_payload = json.loads(warn_line[len("DISPATCH-LEDGER-WARN ") :])
    assert warn_payload["dispatch_id"] == args.dispatch_id
    assert warn_payload["worker_pid"] == spawned_worker.pid
    assert warn_payload["spawn_state"] == "spawned"
    assert warn_payload["tail"] == str(tmp_path / f"{args.dispatch_id}.tail")
    assert warn_payload["status_json"] == str(tmp_path / f"{args.dispatch_id}.status.json")

    assert "export" in export_calls
    assert "registry" in export_calls


def test_explicit_model_is_durable_in_running_ledger_record(
    tmp_path: Path,
    spawned_worker: subprocess.Popen,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    args = _dispatch_args(project, "durable-model")
    args.model = "grok-build"
    _prepare_starting_attempt(args, project, tmp_path)
    L.claim_attempt_running(project, args.dispatch_id, spawned_worker.pid)

    assert _record(
        args,
        project,
        tmp_path,
        worker_pid=spawned_worker.pid,
        state="running",
    ) is None

    assert _ledger_record(args.dispatch_id)["model"] == "grok-build"


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


def test_startup_race_reuses_one_reader_with_capped_poll_backoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = [0.0]
    open_calls: list[dict] = []
    reader = SimpleNamespace(
        attempt_for_dispatch=lambda _dispatch_id: SimpleNamespace(
            lifecycle_state=L.goalflight_journal.ATTEMPT_STARTING
        )
    )

    def monotonic() -> float:
        return clock[0]

    def sleep(delay: float) -> None:
        clock[0] += delay

    def open_reader(_cls, project_root, **kwargs):
        open_calls.append({"project_root": project_root, "kwargs": kwargs})
        return reader

    monkeypatch.setattr(L.time, "monotonic", monotonic)
    monkeypatch.setattr(L.time, "sleep", sleep)
    monkeypatch.setattr(
        L.goalflight_journal.Journal,
        "open_reader",
        classmethod(open_reader),
    )

    code, refusal = L.retry_record_after_startup_race(
        lambda: (1, {"reason": "attempt_not_yet_running"}),
        1,
        {"reason": "attempt_not_yet_running"},
        project_root=tmp_path,
        dispatch_id="reader-reuse",
        timeout_s=2.0,
        poll_s=0.05,
    )

    assert code == 1
    assert refusal == {"reason": "attempt_not_yet_running"}
    assert len(open_calls) == 1
    assert open_calls[0]["kwargs"] == {"persistent": True}
    assert clock[0] == 2.0


def test_startup_race_retries_initial_reader_open_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = [0.0]
    open_calls: list[dict] = []
    reader = SimpleNamespace(
        attempt_for_dispatch=lambda _dispatch_id: SimpleNamespace(
            lifecycle_state=L.goalflight_journal.ATTEMPT_STARTING
        )
    )

    def monotonic() -> float:
        return clock[0]

    def sleep(delay: float) -> None:
        clock[0] += delay

    def open_reader(_cls, project_root, **kwargs):
        open_calls.append({"project_root": project_root, "kwargs": kwargs})
        if len(open_calls) == 1:
            raise OSError("journal startup race")
        return reader

    monkeypatch.setattr(L.time, "monotonic", monotonic)
    monkeypatch.setattr(L.time, "sleep", sleep)
    monkeypatch.setattr(
        L.goalflight_journal.Journal,
        "open_reader",
        classmethod(open_reader),
    )

    code, refusal = L.retry_record_after_startup_race(
        lambda: (1, {"reason": "attempt_not_yet_running"}),
        1,
        {"reason": "attempt_not_yet_running"},
        project_root=tmp_path,
        dispatch_id="reader-open-race",
        timeout_s=2.0,
        poll_s=0.05,
    )

    assert code == 1
    assert refusal == {"reason": "attempt_not_yet_running"}
    assert len(open_calls) == 2


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
    assert status["spawn_state"] == "spawned"
    assert (
        status["ledger_record_warning"]["disposition"] == "attempt_not_yet_running"
    )

    err = capsys.readouterr().err
    assert "DISPATCH-LEDGER-WARN" in err
    assert "attempt_not_yet_running" in err
    warn_line = next(
        line for line in err.splitlines() if line.startswith("DISPATCH-LEDGER-WARN ")
    )
    warn_payload = json.loads(warn_line[len("DISPATCH-LEDGER-WARN ") :])
    assert warn_payload["worker_pid"] == spawned_worker.pid
    assert warn_payload["spawn_state"] == "spawned"
    assert warn_payload["tail"] == str(tmp_path / f"{args.dispatch_id}.tail")

    assert "export" in export_calls
    assert "registry" in export_calls


def test_no_worker_spawned_refusal_still_raises(
    tmp_path: Path,
    export_calls: list[str],
    capsys: pytest.CaptureFixture,
) -> None:
    """No worker + refusal: raise and keep the parsed cause in status."""
    project = tmp_path / "project"
    project.mkdir()
    args = _dispatch_args(project, "t377-no-worker")
    args.status_json = str(tmp_path / f"{args.dispatch_id}.status.json")
    args.tail = str(tmp_path / f"{args.dispatch_id}.tail")
    _record(args, project, tmp_path, worker_pid=None, state="waiting_capacity")
    export_calls.clear()
    capsys.readouterr()

    # A separate same-id launcher has already crossed PREPARED -> STARTING.
    # The loser must still be refused, and now the actual CAS reason is kept.
    authority = J.open_or_create_journal(project)
    attempt = authority.attempt_for_dispatch(args.dispatch_id)
    assert attempt is not None
    assert authority.start_attempt(
        attempt.attempt_id,
        attempt.launch_token,
        expected_launch_epoch=attempt.launch_epoch,
    ).committed
    with pytest.raises(
        RuntimeError, match="cas_lost.*PREPARED -> STARTING lost"
    ) as refusal:
        _record(args, project, tmp_path, worker_pid=None, state="starting")

    D._record_prelaunch_failure(
        args, error=refusal.value, allow_existing_record=True
    )
    status = _status_payload(tmp_path, args.dispatch_id)
    assert status["state"] == "prelaunch_failed"
    assert "cas_lost" in status["reason"]
    assert "PREPARED -> STARTING lost" in status["reason"]
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
    assert warning["spawn_state"] == "unknown"
    status = _status_payload(tmp_path, args.dispatch_id)
    assert status["ledger_record_warning"]["disposition"] == "cas_lost"
    assert status["ledger_record_warning"]["spawn_state"] == "unknown"
    assert status["spawn_state"] == "unknown"
    assert status["worker_alive"] is None  # honestly unknown
    err = capsys.readouterr().err
    assert "DISPATCH-LEDGER-WARN" in err
    warn_line = next(
        line for line in err.splitlines() if line.startswith("DISPATCH-LEDGER-WARN ")
    )
    warn_payload = json.loads(warn_line[len("DISPATCH-LEDGER-WARN ") :])
    assert warn_payload["worker_pid"] == "not-a-pid"
    assert warn_payload["spawn_state"] == "unknown"
    assert warn_payload["tail"] == str(tmp_path / f"{args.dispatch_id}.tail")
    assert "export" in export_calls
    assert "registry" in export_calls


def test_acp_spawned_worker_genuine_refusal_writes_status_and_warns(
    tmp_path: Path,
    spawned_worker: subprocess.Popen,
    capsys: pytest.CaptureFixture,
) -> None:
    """ACP copy of the spawn split: post-spawn refusal warns and writes status."""
    project = tmp_path / "project"
    project.mkdir()
    dispatch_id = "t377-acp-genuine-refusal"
    cfg = _acp_cfg(dispatch_id)
    _record_acp(
        cfg, project, tmp_path, dispatch_id=dispatch_id, worker_pid=None, state="starting"
    )
    capsys.readouterr()

    cfg.queue_launch_token = "bogus-token"
    warning = _record_acp(
        cfg,
        project,
        tmp_path,
        dispatch_id=dispatch_id,
        worker_pid=spawned_worker.pid,
        state="running",
    )

    assert warning is not None
    assert warning["disposition"] == "cas_lost"
    assert warning["spawn_state"] == "spawned"
    status = _status_payload(tmp_path, dispatch_id)
    assert status["ledger_record_warning"]["disposition"] == "cas_lost"
    assert status["spawn_state"] == "spawned"
    err = capsys.readouterr().err
    assert "goalflight_acp_run: WARN:" in err
    assert "cas_lost" in err
    assert f"pid {spawned_worker.pid}" in err
    assert str(tmp_path / f"{dispatch_id}.status.json") in err


def test_explicit_acp_model_is_durable_in_ledger_record(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    dispatch_id = "durable-acp-model"
    cfg = _acp_cfg(dispatch_id)
    cfg.model = "grok-build"

    _record_acp(
        cfg, project, tmp_path, dispatch_id=dispatch_id, worker_pid=None, state="starting"
    )

    assert _ledger_record(dispatch_id)["model"] == "grok-build"


def test_acp_no_worker_spawned_refusal_still_raises(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """ACP copy: no worker + refusal still raises and skips the status write."""
    project = tmp_path / "project"
    project.mkdir()
    dispatch_id = "t377-acp-no-worker"
    cfg = _acp_cfg(dispatch_id)
    _record_acp(
        cfg, project, tmp_path, dispatch_id=dispatch_id, worker_pid=None, state="starting"
    )
    capsys.readouterr()

    cfg.queue_launch_token = "bogus-token"
    with pytest.raises(RuntimeError, match="journal attempt transition refused"):
        _record_acp(
            cfg,
            project,
            tmp_path,
            dispatch_id=dispatch_id,
            worker_pid=None,
            state="running",
        )

    assert not (tmp_path / f"{dispatch_id}.status.json").exists()
    assert "goalflight_acp_run: WARN:" not in capsys.readouterr().err


def test_review_spawned_worker_genuine_refusal_warns_without_raising(
    tmp_path: Path,
    spawned_worker: subprocess.Popen,
    capsys: pytest.CaptureFixture,
) -> None:
    """Review copy: post-spawn refusal warns and returns; the caller writes status.

    The helper itself does not write status.json (unlike ACP/dispatch). That is
    the production split: main() folds the warning into the status payload it
    writes immediately after spawn. Equivalence is the spawn_state branch, not
    which frame performs the write.
    """
    project = tmp_path / "project"
    project.mkdir()
    dispatch_id = "t377-review-genuine-refusal"
    args = _review_args(project, tmp_path, "t377-review-genuine")
    _record_review(
        args, tmp_path, dispatch_id=dispatch_id, worker_pid=None, state="starting"
    )
    capsys.readouterr()

    # Real CAS loss: a second record with a different engine is refused by the
    # journal, not by a stubbed helper.
    args.agent = "claude"
    warning = _record_review(
        args,
        tmp_path,
        dispatch_id=dispatch_id,
        worker_pid=spawned_worker.pid,
        state="running",
    )

    assert warning is not None
    assert warning["disposition"] == "cas_lost"
    assert warning["spawn_state"] == "spawned"
    assert not (tmp_path / f"{dispatch_id}.status.json").exists()
    stdout_path = tmp_path / f"{dispatch_id}.stdout.jsonl"
    stderr_path = tmp_path / f"{dispatch_id}.stderr.log"
    err = capsys.readouterr().err
    assert "goalflight_review_job: WARN:" in err
    assert "cas_lost" in err
    assert f"pid {spawned_worker.pid}" in err
    assert str(stdout_path) in err
    assert str(stderr_path) in err


def test_review_no_worker_spawned_refusal_still_raises(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Review copy: no worker + refusal still raises."""
    project = tmp_path / "project"
    project.mkdir()
    dispatch_id = "t377-review-no-worker"
    args = _review_args(project, tmp_path, "t377-review-none")
    _record_review(
        args, tmp_path, dispatch_id=dispatch_id, worker_pid=None, state="starting"
    )
    capsys.readouterr()

    args.agent = "claude"
    with pytest.raises(RuntimeError, match="journal attempt transition refused"):
        _record_review(
            args, tmp_path, dispatch_id=dispatch_id, worker_pid=None, state="running"
        )

    assert not (tmp_path / f"{dispatch_id}.status.json").exists()
    assert "goalflight_review_job: WARN:" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "module_name",
    [
        "test_dispatch_running_record_refusal",
        "test_codex_dispatch_seams",
        "test_acp_reexec",
        "test_acp_model_passthrough",
    ],
)
@pytest.mark.parametrize("configured_python", [None, "/configured/acp/python"])
def test_importing_module_does_not_mutate_acp_python_env(
    tmp_path: Path,
    module_name: str,
    configured_python: str | None,
) -> None:
    """Collection must not pin GOALFLIGHT_ACP_PYTHON on the process.

    An import-time os.environ write leaks into every later module in the same
    pytest process via env.copy() child launches. Scope overrides to a fixture
    or patch context so teardown restores them.
    """
    env = os.environ.copy()
    env.pop("GOALFLIGHT_ACP_PYTHON", None)
    if configured_python is not None:
        env["GOALFLIGHT_ACP_PYTHON"] = configured_python
    env["GOALFLIGHT_STATE_DIR"] = str(tmp_path / "state")
    env["GOALFLIGHT_DISPATCH_DIR"] = str(tmp_path / "dispatch")
    env["GOALFLIGHT_JOURNAL_DIR"] = str(tmp_path / "journals")
    env["GOALFLIGHT_MESSAGES_DIR"] = str(tmp_path / "messages")
    env["GOALFLIGHT_TASK_STORE"] = str(tmp_path / "task-store")
    env["GOALFLIGHT_TASK_STORE_DIR"] = str(tmp_path / "task-store")
    env["GOALFLIGHT_WAKE_LEDGER"] = str(tmp_path / "wake-ledger")
    env["GOALFLIGHT_PIDFILE_DIR"] = str(tmp_path / "pids")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp_path / "pids")
    env["GOALFLIGHT_CAPACITY_CONF"] = "/dev/null"
    env["PYTHONPATH"] = str(ROOT / "scripts")
    probe = (
        "import os, sys\n"
        f"sys.path.insert(0, {str(ROOT / 'tests' / 'python')!r})\n"
        f"assert os.environ.get('GOALFLIGHT_ACP_PYTHON') == {configured_python!r}\n"
        f"import {module_name} as m\n"
        "print('after=' + repr(os.environ.get('GOALFLIGHT_ACP_PYTHON')))\n"
        "print('module=' + m.__name__)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert f"after={configured_python!r}" in proc.stdout
