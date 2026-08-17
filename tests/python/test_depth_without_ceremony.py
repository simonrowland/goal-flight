"""t-249 + t-267 residual: restore listener depth without ceremony.

A live controller must reach target depth without remembering to re-arm.
listen-auto resolves the journal lease (env is one input, not the only one).
relay / status / next emit a one-line depth cue once per transition.
The numbered listen-exit list is frozen and is not repeated on those surfaces.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
TASK = ROOT / "goalflight_task.py"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))

import goalflight_journal as journal  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_wake as wake  # noqa: E402

# Frozen listen-exit numbered list. Do not change this string.
LISTEN_EXIT_HINT_SNAPSHOT = (
    "listener floor: work in flight and live=0/4 — 4 slots missing; "
    "issue each as its own tracked background task; "
    "a shell `&` loop is one untracked call and those wakes reach nobody:\n"
    "1. CMD\n"
    "2. CMD\n"
    "3. CMD\n"
    "4. CMD"
)


@pytest.fixture()
def isolated(monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, str]]:
    td = Path(tempfile.mkdtemp(prefix="gf-depth-ceremony-"))
    env = {
        "GOALFLIGHT_JOURNAL_DIR": str(td / "journals"),
        "GOALFLIGHT_STATE_DIR": str(td / "state"),
        "GOALFLIGHT_WAKE_LEDGER_DIR": str(td / "wake-ledger"),
        "GOALFLIGHT_MESSAGES_DIR": str(td / "messages"),
        "GOALFLIGHT_TASK_STORE_DIR": str(td / "task-store"),
        "GOAL_FLIGHT_PIDFILE_DIR": str(td / "pids"),
        "GOALFLIGHT_CAPACITY_CONF": os.devnull,
        "GOALFLIGHT_TEST_MODE": "1",
        "GOALFLIGHT_TEST_LISTENER_START_TOKEN": "depth-listener-token",
        "GOALFLIGHT_PROCESS_ROLE": "controller",
    }
    for value in env.values():
        if value != os.devnull:
            Path(value).mkdir(parents=True, exist_ok=True)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for key in (
        "GOALFLIGHT_CONTROLLER_LABEL",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE",
        "GOALFLIGHT_CONTROLLER_SESSION_ID",
        "GOALFLIGHT_DISPATCH_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    project = td / "project"
    project.mkdir()
    return project, {**os.environ, **env}


def _claim(project: Path, label: str = "depth-ctl", **kwargs) -> journal.LeaseIdentity:
    authority = journal.open_or_create_journal(project)
    claimed = authority.claim_or_renew_lease(
        label, principal={"principal_id": f"{label}-principal"}, **kwargs
    )
    assert claimed.committed and claimed.value is not None
    return claimed.value


def _work(project: Path, dispatch_id: str = "depth-work") -> None:
    authority = journal.open_or_create_journal(project)
    assert authority.prepare_attempt(dispatch_id).committed


def _generation_hash(nonce: str) -> str:
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()[:16]


def _slot_generation_hashes(project: Path, label: str) -> set[str]:
    prefix = f"listener-slot-v1.{wake._label_hash(label)}."
    found: set[str] = set()
    directory = wake.ledger_dir(project)
    if not directory.is_dir():
        return found
    for path in directory.iterdir():
        if not path.name.startswith(prefix) or not path.name.endswith(".lock"):
            continue
        parts = path.name.split(".")
        if len(parts) >= 3:
            found.add(parts[2])
    return found


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


def _listen_auto_cmd(project: Path, *, label: str, nonce: str | None = None) -> list[str]:
    cmd = [
        sys.executable,
        str(SCRIPTS / "goalflight_messages.py"),
        "listen-auto",
        "--project-root",
        str(project),
        "--controller-label",
        label,
        "--poll-secs",
        "0.05",
        "--timeout-s",
        "20",
        "--report-pending",
    ]
    if nonce:
        cmd.extend(["--lease-nonce", nonce])
    return cmd


def _arm_listen_auto(
    project: Path,
    env: dict[str, str],
    *,
    label: str,
    nonce: str | None = None,
) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        _listen_auto_cmd(project, label=label, nonce=nonce),
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc


def _run(project: Path, env: dict[str, str], argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def _write_ready_task(project: Path) -> None:
    docs = project / "docs-private"
    docs.mkdir(parents=True, exist_ok=True)
    docs.joinpath("tasks.jsonl").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "t-001",
                "kind": "task",
                "title": "Ready work",
                "blocked_by": [],
                "links": [],
                "done": False,
                "created_at": "2026-08-17T00:00:00+00:00",
                "created_by": "test",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def test_listen_exit_numbered_hint_is_frozen() -> None:
    assert (
        wake.listener_floor_hint(0, 4, "CMD", work_in_flight=True)
        == LISTEN_EXIT_HINT_SNAPSHOT
    )


def test_activity_hint_is_one_line_and_silent_when_done() -> None:
    command = "python3 scripts/goalflight_messages.py listen-auto --report-pending"
    hint = wake.listener_activity_hint(0, 4, command, work_in_flight=True)
    assert hint == f"listener depth 0/4 — 4 missing; {command}"
    assert "\n" not in hint
    assert "1. " not in hint
    assert wake.listener_activity_hint(0, 4, command, work_in_flight=False) == ""
    assert wake.listener_activity_hint(4, 4, command, work_in_flight=True) == ""


def test_listen_auto_arms_without_capability_env(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    """Observed failure: claimed lease, no env nonce, listen-auto refused."""
    project, env = isolated
    lease = _claim(project)
    _work(project)
    assert "GOALFLIGHT_CONTROLLER_LEASE_NONCE" not in env
    assert "GOALFLIGHT_CONTROLLER_SESSION_ID" not in env
    with wake.register_lease_holder(
        project, controller_label="depth-ctl", lease_nonce=lease.nonce
    ):
        proc = _arm_listen_auto(project, env, label="depth-ctl")
        try:
            _wait_live(project, "depth-ctl", 1)
            live = wake.live_waiters(project, controller_label="depth-ctl") or []
            assert len(live) == 1
            assert _generation_hash(lease.nonce) in _slot_generation_hashes(
                project, "depth-ctl"
            )
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=5)


def test_two_live_generations_do_not_silently_pick_the_wrong_one(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    first = _claim(project)
    _work(project)
    with wake.register_lease_holder(
        project, controller_label="depth-ctl", lease_nonce=first.nonce
    ):
        second = authority.claim_or_renew_lease(
            "depth-ctl",
            principal={"principal_id": "depth-ctl-takeover"},
            takeover=True,
        )
        assert second.committed and second.value is not None
        replacement = second.value
        assert replacement.nonce != first.nonce
        with wake.register_lease_holder(
            project,
            controller_label="depth-ctl",
            lease_nonce=replacement.nonce,
        ):
            env_old = dict(env)
            env_old["GOALFLIGHT_CONTROLLER_LEASE_NONCE"] = first.nonce
            refused = _run(
                project,
                env_old,
                _listen_auto_cmd(project, label="depth-ctl"),
            )
            assert refused.returncode == 2, refused.stderr
            assert "ambiguous-controller-generation" in refused.stderr
            assert (wake.live_waiters(project, controller_label="depth-ctl") or []) == []

            pinned = _arm_listen_auto(
                project, env, label="depth-ctl", nonce=replacement.nonce
            )
            try:
                _wait_live(project, "depth-ctl", 1)
                hashes = _slot_generation_hashes(project, "depth-ctl")
                assert _generation_hash(replacement.nonce) in hashes
                assert _generation_hash(first.nonce) not in hashes
            finally:
                if pinned.poll() is None:
                    pinned.send_signal(signal.SIGTERM)
                    pinned.wait(timeout=5)


def test_self_resolution_uses_active_generation_when_env_is_absent(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    authority = journal.open_or_create_journal(project)
    first = _claim(project)
    _work(project)
    with wake.register_lease_holder(
        project, controller_label="depth-ctl", lease_nonce=first.nonce
    ):
        second = authority.claim_or_renew_lease(
            "depth-ctl",
            principal={"principal_id": "depth-ctl-takeover-2"},
            takeover=True,
        )
        assert second.committed and second.value is not None
        replacement = second.value
        with wake.register_lease_holder(
            project,
            controller_label="depth-ctl",
            lease_nonce=replacement.nonce,
        ):
            proc = _arm_listen_auto(project, env, label="depth-ctl")
            try:
                _wait_live(project, "depth-ctl", 1)
                hashes = _slot_generation_hashes(project, "depth-ctl")
                assert _generation_hash(replacement.nonce) in hashes
                assert _generation_hash(first.nonce) not in hashes
            finally:
                if proc.poll() is None:
                    proc.send_signal(signal.SIGTERM)
                    proc.wait(timeout=5)


def _activity_env(env: dict[str, str], *, label: str = "depth-ctl") -> dict[str, str]:
    # Label is known; the nonce is what Bash tool calls drop.
    activity = dict(env)
    activity["GOALFLIGHT_CONTROLLER_LABEL"] = label
    activity.pop("GOALFLIGHT_CONTROLLER_LEASE_NONCE", None)
    activity.pop("GOALFLIGHT_CONTROLLER_SESSION_ID", None)
    return activity


def _activity_surfaces(
    project: Path, env: dict[str, str]
) -> list[tuple[str, subprocess.CompletedProcess[str]]]:
    _write_ready_task(project)
    env = _activity_env(env)
    relay = _run(
        project,
        env,
        [sys.executable, str(SCRIPTS / "goalflight_messages.py"), "relay", "--drain"],
    )
    status = _run(
        project,
        env,
        [
            sys.executable,
            str(SCRIPTS / "goalflight_session_status.py"),
            "--project-root",
            str(project),
            "--json",
        ],
    )
    nxt = _run(
        project,
        env,
        [sys.executable, str(TASK), "--project-root", str(project), "next"],
    )
    dash = _run(
        project,
        env,
        [
            sys.executable,
            str(SCRIPTS / "goalflight_status.py"),
            "--project",
            str(project),
            "--json",
        ],
    )
    return [("relay", relay), ("status", status), ("next", nxt), ("dashboard", dash)]


def test_activity_surfaces_teach_depth_without_listen_exit(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    lease = _claim(project)
    _work(project)
    with wake.register_lease_holder(
        project, controller_label="depth-ctl", lease_nonce=lease.nonce
    ):
        assert (wake.live_waiters(project, controller_label="depth-ctl") or []) == []
        surfaces = _activity_surfaces(project, env)
        depth_lines = []
        for name, proc in surfaces:
            assert proc.returncode == 0, f"{name}: {proc.stderr}"
            for line in proc.stderr.splitlines():
                if line.startswith("listener depth "):
                    depth_lines.append((name, line))
        assert depth_lines, "0/4 with work in flight must teach depth on activity"
        name, line = depth_lines[0]
        assert name in {"relay", "status", "next", "dashboard"}
        assert line.startswith("listener depth 0/4 — 4 missing;")
        assert "\n" not in line
        assert "1. " not in line
        later = [row for row in depth_lines[1:]]
        assert later == [], later


def test_relay_drain_adds_one_line_and_status_json_parses(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    lease = _claim(project)
    _work(project)
    with wake.register_lease_holder(
        project, controller_label="depth-ctl", lease_nonce=lease.nonce
    ):
        first = _run(
            project,
            _activity_env(env),
            [
                sys.executable,
                str(SCRIPTS / "goalflight_messages.py"),
                "relay",
                "--drain",
            ],
        )
        assert first.returncode == 0, first.stderr
        assert first.stdout.strip() == "no mail"
        added = [line for line in first.stderr.splitlines() if line.startswith("listener depth ")]
        assert len(added) == 1
        assert added[0].count("\n") == 0
        assert "1. " not in first.stderr
        assert first.stderr.count("\n") == 1 or first.stderr.strip() == added[0]
        assert LISTEN_EXIT_HINT_SNAPSHOT.splitlines()[0] not in first.stderr

        second = _run(
            project,
            _activity_env(env),
            [
                sys.executable,
                str(SCRIPTS / "goalflight_messages.py"),
                "relay",
                "--drain",
            ],
        )
        assert second.returncode == 0, second.stderr
        assert second.stdout.strip() == "no mail"
        assert [
            line
            for line in second.stderr.splitlines()
            if line.startswith("listener depth ")
        ] == []

        status = _run(
            project,
            _activity_env(env),
            [
                sys.executable,
                str(SCRIPTS / "goalflight_session_status.py"),
                "--project-root",
                str(project),
                "--json",
            ],
        )
        assert status.returncode == 0, status.stderr
        json.loads(status.stdout)

        dash = _run(
            project,
            _activity_env(env),
            [
                sys.executable,
                str(SCRIPTS / "goalflight_status.py"),
                "--project",
                str(project),
                "--json",
            ],
        )
        assert dash.returncode == 0, dash.stderr
        json.loads(dash.stdout)


def test_activity_surfaces_stay_silent_at_target_and_without_work(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    lease = _claim(project)
    _work(project)
    holders = [
        wake.register_listener_waiter(
            project,
            controller_label="depth-ctl",
            generation_key=lease.nonce,
        )
        for _ in range(wake.DEFAULT_LISTENER_SLOTS)
    ]
    try:
        live = wake.live_waiters(project, controller_label="depth-ctl") or []
        assert len(live) == wake.DEFAULT_LISTENER_SLOTS
        for name, proc in _activity_surfaces(project, env):
            assert proc.returncode == 0, f"{name}: {proc.stderr}"
            assert [
                line
                for line in proc.stderr.splitlines()
                if line.startswith("listener depth ")
            ] == [], name
    finally:
        for holder in holders:
            holder.close()


def test_activity_surfaces_stay_silent_without_in_flight_work(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, env = isolated
    lease = _claim(project)
    with wake.register_lease_holder(
        project, controller_label="depth-ctl", lease_nonce=lease.nonce
    ):
        for name, proc in _activity_surfaces(project, env):
            assert proc.returncode == 0, f"{name}: {proc.stderr}"
            assert [
                line
                for line in proc.stderr.splitlines()
                if line.startswith("listener depth ")
            ] == [], name


def test_activity_signal_emits_once_per_transition(
    isolated: tuple[Path, dict[str, str]],
) -> None:
    project, _env = isolated
    plan = wake.listener_depth_plan(0, 4, "CMD", work_in_flight=True)
    first = wake.consume_listener_activity_signal(project, "depth-ctl", plan)
    second = wake.consume_listener_activity_signal(project, "depth-ctl", plan)
    assert first.startswith("listener depth 0/4 — 4 missing;")
    assert second == ""
    recovered = wake.listener_depth_plan(4, 4, "CMD", work_in_flight=True)
    assert wake.consume_listener_activity_signal(project, "depth-ctl", recovered) == ""
    dropped = wake.listener_depth_plan(0, 4, "CMD", work_in_flight=True)
    assert wake.consume_listener_activity_signal(
        project, "depth-ctl", dropped
    ).startswith("listener depth 0/4")
