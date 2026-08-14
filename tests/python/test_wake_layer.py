"""Held-lock wake coverage, poll fallback, and universal entry contracts."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _env = isolated
    registration = wake.register_waiter(
        root,
        controller_label="wake-test",
        kind="wait",
    )
    stale_path = registration.record.path
    registration.close()
    stale_path.touch()
    samples = iter((True, False))
    monkeypatch.setattr(wake, "_probe_locked_once", lambda _path: next(samples))
    monkeypatch.setattr(wake.time, "sleep", lambda _seconds: None)

    assert wake.live_waiters(root, controller_label="wake-test") == []
    assert not stale_path.exists()


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


def test_listener_and_status_wait_both_join_the_waiter_pool(
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
                for row in wake.live_waiters(root, controller_label="wake-test")
            }
            if kinds == {"listener", "wait"}:
                break
            time.sleep(0.05)
        assert kinds == {"listener", "wait"}
    finally:
        for process in (listener, long_wait):
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3)
    assert wake.live_waiters(root, controller_label="wake-test") == []


def test_bad_rearm_token_does_not_displace_healthy_listener(
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
        assert bad.returncode in {2, 3}
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
