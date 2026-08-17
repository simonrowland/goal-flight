"""t-272 + t-273: terse controller-startup JSON, and arming over pending mail.

t-272: ``--controller-startup`` is a machine surface. Before e333cc5 it was
JSON-only. The floor work put the numbered human list on that surface three
times (stderr + commands[] + hint), repeating a long project path twelve
times. The numbered list stays on listen-exit, where it is pedagogical.
Startup JSON carries one command template and no rendered prose.

t-273 design (argued, not picked by habit):

(a) Floor hint says "drain first" when mail is pending. Rejected as the
    primary fix: it documents the footgun and still lets a caller arm into
    a zero-depth pool.

(b) ``--report-pending`` reports on the first listener only; the rest keep
    waiting. Chosen. One tracked task per slot, no new drain-then-arm
    ordering rule. Pending mail becomes one report, not four exits.

    Two arms racing: ``claim_pending_report`` exclusive-creates a per-lease
    stamp. Exactly one wins and emits. The loser still raises its local
    high-water from its own peek, so the same backlog cannot pop the rest
    of the pool. If mail arrives between the two peeks, the earlier
    listener has the older high-water and still rings; the later one is
    conservative about that in-flight item. Depth stays at target until
    that one ring.

(c) An arm-to-depth helper that drains once then forks N waiters. Rejected:
    one tracked parent of N children is the untracked-stray shape the
    floor exists to prevent, and a silent cursor advance would mark mail
    read before anyone processed it.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_session_status as sessions  # noqa: E402
import goalflight_wake as wake  # noqa: E402

# Frozen listen-exit numbered list. Changing this string is the regression
# the operator asked us not to trade for the terse-startup fix.
LISTEN_EXIT_HINT_SNAPSHOT = (
    "listener floor: work in flight and live=0/4 — 4 slots missing; "
    "issue each as its own tracked background task; "
    "a shell `&` loop is one untracked call and those wakes reach nobody:\n"
    "1. CMD\n"
    "2. CMD\n"
    "3. CMD\n"
    "4. CMD"
)
LISTEN_EXIT_THIN_HINT_SNAPSHOT = (
    "listener pool n=1/4 — 3 slots missing; "
    "issue each as its own tracked background task; "
    "a shell `&` loop is one untracked call and those wakes reach nobody:\n"
    "1. CMD\n"
    "2. CMD\n"
    "3. CMD"
)

LISTENER_DEPTH_KEYS = {
    "live",
    "target",
    "missing",
    "work_in_flight",
    "command",
    "separate_tracked_tasks",
}


@pytest.fixture()
def isolated(monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, str]]:
    td = Path(tempfile.mkdtemp(prefix="gf-terse-startup-"))
    env = {
        "GOALFLIGHT_JOURNAL_DIR": str(td / "journals"),
        "GOALFLIGHT_STATE_DIR": str(td / "state"),
        "GOALFLIGHT_WAKE_LEDGER_DIR": str(td / "wake-ledger"),
        "GOALFLIGHT_MESSAGES_DIR": str(td / "messages"),
        "GOALFLIGHT_TASK_STORE_DIR": str(td / "task-store"),
        "GOAL_FLIGHT_PIDFILE_DIR": str(td / "pids"),
        "GOALFLIGHT_CAPACITY_CONF": os.devnull,
        "GOALFLIGHT_TEST_MODE": "1",
        "GOALFLIGHT_PROCESS_ROLE": "controller",
    }
    for value in env.values():
        if value != os.devnull:
            Path(value).mkdir(parents=True, exist_ok=True)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GOALFLIGHT_CONTROLLER_LABEL", raising=False)
    monkeypatch.delenv("GOALFLIGHT_CONTROLLER_LEASE_NONCE", raising=False)
    monkeypatch.delenv("GOALFLIGHT_CONTROLLER_SESSION_ID", raising=False)
    monkeypatch.delenv("GOALFLIGHT_DISPATCH_ID", raising=False)
    project = td / "project"
    project.mkdir()
    return project, {**os.environ, **env}


def _claim(project: Path, label: str = "terse-ctl") -> journal.LeaseIdentity:
    authority = journal.open_or_create_journal(project)
    claimed = authority.claim_or_renew_lease(
        label, principal={"principal_id": f"{label}-principal"}
    )
    assert claimed.committed and claimed.value is not None
    return claimed.value


def _post(env: dict[str, str], project: Path, label: str, text: str) -> None:
    subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "goalflight_messages.py"),
            "post",
            "--to-controller",
            label,
            "--dispatch-id",
            "terse-mail",
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


def _listen_cmd(
    project: Path,
    *,
    label: str,
    nonce: str,
    timeout_s: float = 20,
    json_out: bool = False,
) -> list[str]:
    cmd = [
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
        "0.05",
        "--timeout-s",
        str(timeout_s),
        "--report-pending",
    ]
    if json_out:
        cmd.append("--json")
    return cmd


def _wait_live(project: Path, label: str, count: int, *, timeout_s: float = 6) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        waiters = wake.live_waiters(project, controller_label=label) or []
        if len(waiters) == count:
            return
        time.sleep(0.02)
    raise AssertionError(
        f"live waiters for {label} never reached {count}; "
        f"saw {len(wake.live_waiters(project, controller_label=label) or [])}"
    )


def test_listen_exit_numbered_hint_is_unchanged() -> None:
    assert (
        wake.listener_floor_hint(0, 4, "CMD", work_in_flight=True)
        == LISTEN_EXIT_HINT_SNAPSHOT
    )
    assert (
        wake.listener_floor_hint(1, 4, "CMD", work_in_flight=True)
        == LISTEN_EXIT_THIN_HINT_SNAPSHOT
    )
    assert wake.listener_floor_hint(0, 4, "CMD", work_in_flight=False) == ""
    assert wake.listener_floor_hint(4, 4, "CMD", work_in_flight=True) == ""


def test_controller_startup_stdout_is_json_without_preprocessing(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    assert authority.prepare_attempt("terse-startup-work").committed
    host = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPTS / "goalflight_session_status.py"),
                "--project-root",
                str(project),
                "--controller-startup",
                "--session-pid",
                str(host.pid),
                "--session-label",
                "terse-ctl",
            ],
            cwd=project,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdout is not None
        # Mechanical t-272 proof: no strip, no line skip, no 2>&1.
        payload = json.load(proc.stdout)
        stderr = proc.stderr.read() if proc.stderr is not None else ""
        assert proc.wait(timeout=8) == 0
    finally:
        host.kill()
        host.wait()

    assert payload["claimed"] is True
    depth = payload["listener_depth"]
    assert set(depth) == LISTENER_DEPTH_KEYS
    assert isinstance(depth["command"], str) and depth["command"]
    assert depth["command"].count(str(project.resolve())) == 1
    stdout_dump = json.dumps(payload)
    assert stdout_dump.count(str(project.resolve())) == 1
    assert "listener floor:" not in stdout_dump
    assert "listener floor:" not in stderr
    assert "1. " not in stderr


def test_controller_startup_json_is_terse_with_work_in_flight(
    isolated: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _env = isolated
    authority = journal.open_or_create_journal(project)
    assert authority.prepare_attempt("terse-claim-work").committed
    monkeypatch.setattr(
        sessions,
        "_controller_process_identity",
        lambda pid: {"pid": pid, "start_token": "terse-claim-token"},
    )
    result = sessions.claim_controller_startup(
        project, pid=81001, label="terse-ctl", role="controller"
    )
    assert result["claimed"] is True
    depth = result["listener_depth"]
    assert set(depth) == LISTENER_DEPTH_KEYS
    assert depth["work_in_flight"] is True
    assert depth["live"] == 0
    assert depth["missing"] == wake.DEFAULT_LISTENER_SLOTS
    assert depth["separate_tracked_tasks"] is True
    encoded = json.dumps(result)
    assert encoded.count(str(project.resolve())) == 1


def test_listen_exit_still_prints_the_numbered_hint(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    lease = _claim(project)
    assert authority.prepare_attempt("terse-exit-work").committed
    with wake.register_lease_holder(
        project, controller_label="terse-ctl", lease_nonce=lease.nonce
    ):
        cmd = _listen_cmd(project, label="terse-ctl", nonce=lease.nonce)
        proc = subprocess.Popen(
            cmd,
            cwd=project,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _wait_live(project, "terse-ctl", 1)
            _post(env, project, "terse-ctl", "ring me")
            stdout, stderr = proc.communicate(timeout=30)
            assert proc.returncode == 0, stderr
            assert stdout.startswith("mail available; peek:")
            expected = wake.listener_floor_hint(
                0,
                wake.DEFAULT_LISTENER_SLOTS,
                shlex.join(cmd),
                work_in_flight=True,
            )
            assert expected
            assert expected in stderr
            assert "1. " in expected
            assert f"{wake.DEFAULT_LISTENER_SLOTS}. " in expected
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def test_claim_pending_report_first_writer_wins(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, _env = isolated
    assert (
        wake.claim_pending_report(
            project, controller_label="terse-ctl", lease_nonce="nonce-a"
        )
        is True
    )
    assert (
        wake.claim_pending_report(
            project, controller_label="terse-ctl", lease_nonce="nonce-a"
        )
        is False
    )
    assert (
        wake.claim_pending_report(
            project, controller_label="terse-ctl", lease_nonce="nonce-b"
        )
        is True
    )


def test_arming_over_pending_mail_reaches_target_depth(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    """The defect: pending mail used to satisfy every arm, leaving 0/N.

    Assert on the resulting depth, not on the launches.
    """
    project, env = isolated
    lease = _claim(project)
    _post(env, project, "terse-ctl", "already waiting")
    target = wake.DEFAULT_LISTENER_SLOTS
    procs: list[subprocess.Popen[str]] = []
    out_paths = [project.parent / f"arm-{index}.stdout" for index in range(target)]
    err_paths = [project.parent / f"arm-{index}.stderr" for index in range(target)]
    with wake.register_lease_holder(
        project, controller_label="terse-ctl", lease_nonce=lease.nonce
    ):
        try:
            for index in range(target):
                out_handle = out_paths[index].open("w", encoding="utf-8")
                err_handle = err_paths[index].open("w", encoding="utf-8")
                procs.append(
                    subprocess.Popen(
                        _listen_cmd(
                            project,
                            label="terse-ctl",
                            nonce=lease.nonce,
                            json_out=True,
                        ),
                        cwd=project,
                        env=env,
                        stdout=out_handle,
                        stderr=err_handle,
                    )
                )
                out_handle.close()
                err_handle.close()
            _wait_live(project, "terse-ctl", target)
            time.sleep(0.3)
            live = wake.live_waiters(project, controller_label="terse-ctl") or []
            assert len(live) == target
            assert all(proc.poll() is None for proc in procs)

            reports = 0
            for path in out_paths:
                text = path.read_text(encoding="utf-8")
                if not text.strip():
                    continue
                for line in text.splitlines():
                    if not line.strip():
                        continue
                    payload = json.loads(line)
                    if payload.get("kind") == "pending-at-arm":
                        reports += 1
                    else:
                        raise AssertionError(
                            f"non-report output while pool should stay armed: {payload}"
                        )
            assert reports == 1
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()


def test_two_racing_arms_yield_one_report_and_stay_live(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    lease = _claim(project)
    _post(env, project, "terse-ctl", "race backlog")
    procs: list[subprocess.Popen[str]] = []
    out_paths = [project.parent / "race-0.stdout", project.parent / "race-1.stdout"]
    with wake.register_lease_holder(
        project, controller_label="terse-ctl", lease_nonce=lease.nonce
    ):
        try:
            for path in out_paths:
                handle = path.open("w", encoding="utf-8")
                procs.append(
                    subprocess.Popen(
                        _listen_cmd(
                            project,
                            label="terse-ctl",
                            nonce=lease.nonce,
                            json_out=True,
                        ),
                        cwd=project,
                        env=env,
                        stdout=handle,
                        stderr=subprocess.DEVNULL,
                    )
                )
                handle.close()
            _wait_live(project, "terse-ctl", 2)
            time.sleep(0.3)
            assert len(wake.live_waiters(project, controller_label="terse-ctl") or []) == 2
            assert all(proc.poll() is None for proc in procs)
            reports = 0
            for path in out_paths:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip() and json.loads(line).get("kind") == "pending-at-arm":
                        reports += 1
            assert reports == 1
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
