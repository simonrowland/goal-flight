"""PID/start-token principals are verified within journal lease generations."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

import goalflight_compat as compat  # noqa: E402
import goalflight_journal as journal  # noqa: E402
import goalflight_session_status as sessions  # noqa: E402
import goalflight_wake as wake  # noqa: E402


def _root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    name: str = "project",
) -> Path:
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE_DIR", str(tmp_path / "task-store"))
    monkeypatch.setenv("GOALFLIGHT_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_WAKE_LEDGER_DIR", str(tmp_path / "wake-ledger"))
    monkeypatch.setenv("GOALFLIGHT_MESSAGES_DIR", str(tmp_path / "messages"))
    monkeypatch.setenv("GOAL_FLIGHT_PIDFILE_DIR", str(tmp_path / "pidfiles"))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", "/dev/null")
    root = tmp_path / name
    root.mkdir()
    return root


def test_controller_startup_adopts_live_incumbent_label_and_advertises_reseat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _root(monkeypatch, tmp_path, name="beta")
    advertised = tmp_path / "advertised-skill"
    monkeypatch.setenv("GOALFLIGHT_ROOT", str(advertised))
    env = dict(os.environ)
    host = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    base_command = [
        sys.executable,
        str(SCRIPTS / "goalflight_session_status.py"),
        "--project-root",
        str(root),
        "--controller-startup",
        "--session-pid",
        str(host.pid),
    ]
    try:
        first = subprocess.run(
            [*base_command, "--session-label", "alpha"],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
        )
        assert first.returncode == 0, first.stderr
        first_payload = json.loads(first.stdout)
        assert first_payload["claimed"] is True
        assert first_payload["session"]["label"] == "alpha"
        assert len(journal.Journal(root).lease_records()) == 1

        adopted = subprocess.run(
            base_command,
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
        )
        assert adopted.returncode == 0, adopted.stderr
        adopted_payload = json.loads(adopted.stdout)
        assert adopted_payload["claimed"] is True
        assert adopted_payload["adopted_label"] == "alpha"
        assert adopted_payload["requested_label"] == "beta"
        assert adopted_payload["session"]["label"] == "alpha"

        active = journal.Journal(root).lease_records()
        assert len(active) == 1
        assert active[0]["label"] == "alpha"
        assert journal.Journal(root).read_all("SELECT 1 FROM delivery_events") == []

        script = str(advertised / "scripts" / "goalflight_session_status.py")
        release = shlex.join(
            [
                "python3",
                script,
                "--project-root",
                str(root.resolve()),
                "--release-session",
                "--session-pid",
                str(host.pid),
            ]
        )
        reclaim = shlex.join(
            [
                "python3",
                script,
                "--project-root",
                str(root.resolve()),
                "--controller-startup",
                "--session-pid",
                str(host.pid),
                "--session-label",
                "beta",
            ]
        )
        notice = (
            "controller startup: adopted existing label 'alpha' for this process; "
            f"if that match is wrong, re-seat with: {release} && {reclaim}"
        )
        assert adopted.stderr.splitlines().count(notice) == 1
        assert str(SCRIPTS / "goalflight_session_status.py") not in notice
    finally:
        host.kill()
        host.wait(timeout=3)


def test_dead_incumbent_is_expired_before_default_label_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _root(monkeypatch, tmp_path, name="beta")
    monkeypatch.setattr(
        sessions,
        "_controller_process_identity",
        lambda pid: {"pid": pid, "start_token": "same-generation"},
    )

    first = sessions.claim_controller_startup(
        root, pid=71001, label="alpha", role="controller"
    )
    assert first["claimed"] is True
    assert first["session"]["label"] == "alpha"
    holder = wake.register_lease_holder(
        root,
        controller_label="alpha",
        lease_nonce=first["session"]["lease_nonce"],
    )
    holder.close()

    second = sessions.claim_controller_startup(
        root, pid=71001, role="controller", environ={}
    )
    assert second["claimed"] is True
    assert second["session"]["label"] == "beta"
    assert "adopted_label" not in second
    active = journal.Journal(root).lease_records()
    assert len(active) == 1
    assert active[0]["label"] == "beta"
    alpha = next(
        row
        for row in journal.Journal(root).lease_records(include_ended=True)
        if row["label"] == "alpha"
    )
    assert alpha["state"] == journal.LEASE_EXPIRED


def test_default_controller_label_is_stable_across_linked_worktrees(
    tmp_path: Path,
) -> None:
    main = tmp_path / "stable-repo"
    linked = tmp_path / "different-worktree-name"
    main.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=main, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=main, check=True
    )
    subprocess.run(["git", "config", "user.name", "Session Test"], cwd=main, check=True)
    (main / "seed").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed"], cwd=main, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=main, check=True)
    subprocess.run(
        ["git", "worktree", "add", "-q", "--detach", str(linked)],
        cwd=main,
        check=True,
    )

    assert sessions.resolve_controller_label(project_root=main, environ={}) == "stable-repo"
    assert sessions.resolve_controller_label(project_root=linked, environ={}) == "stable-repo"


def test_controller_startup_beacon_holds_lock_between_cli_calls_and_drops_with_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _root(monkeypatch, tmp_path)
    env = dict(os.environ)
    host = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    command = [
        sys.executable,
        str(SCRIPTS / "goalflight_session_status.py"),
        "--project-root",
        str(root),
        "--controller-startup",
        "--session-pid",
        str(host.pid),
        "--session-label",
        "controller",
    ]
    try:
        first = subprocess.run(
            command,
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
        )
        assert first.returncode == 0, first.stderr
        first_payload = json.loads(first.stdout)
        assert first_payload["claimed"] is True
        lease = first_payload["session"]
        assert lease["kernel_lock_held"] is True
        assert wake.lease_holder_alive(
            root,
            controller_label="controller",
            lease_nonce=lease["lease_nonce"],
        )

        second = subprocess.run(
            command,
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
        )
        second_payload = json.loads(second.stdout)
        assert second_payload["claimed"] is True
        assert second_payload["session"]["generation"] == lease["generation"]
        assert second_payload["session"]["lease_nonce"] == lease["lease_nonce"]

        host.kill()
        host.wait(timeout=3)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and wake.lease_holder_alive(
            root,
            controller_label="controller",
            lease_nonce=lease["lease_nonce"],
        ):
            time.sleep(0.05)
        assert not wake.lease_holder_alive(
            root,
            controller_label="controller",
            lease_nonce=lease["lease_nonce"],
        )
    finally:
        if host.poll() is None:
            host.kill()
            host.wait(timeout=3)


def test_lock_holder_cheap_ticks_revalidate_only_on_generation_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _root(monkeypatch, tmp_path)
    authority = journal.open_or_create_journal(root)
    claimed = authority.claim_or_renew_lease(
        "controller",
        principal={"pid": 71001, "start_token": "cheap-tick"},
    )
    assert claimed.committed and claimed.value is not None
    lease = claimed.value
    wake.publish_lease_generation_event(
        root,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        generation=lease.generation,
        state=lease.state,
    )

    class Registration:
        closed = False

        def close(self) -> None:
            self.closed = True

    registration = Registration()
    monkeypatch.setattr(wake, "register_lease_holder", lambda *args, **kwargs: registration)
    monkeypatch.setattr(
        sessions,
        "_controller_process_identity",
        lambda pid: {"pid": pid, "start_token": "cheap-tick"},
    )
    monkeypatch.setattr(compat, "process_identity_matches", lambda *_args: True)

    def reject_write_locking_constructor(*_args, **_kwargs):
        raise AssertionError("beacon tick constructed the write-locking Journal client")

    monkeypatch.setattr(journal.Journal, "__init__", reject_write_locking_constructor)
    open_calls: list[int] = []

    def open_reader(_cls, _root):
        open_calls.append(1)
        return authority

    monkeypatch.setattr(journal.Journal, "open_reader", classmethod(open_reader))
    sleeps = {"count": 0}

    def tick(_seconds: float) -> None:
        sleeps["count"] += 1
        if sleeps["count"] == 2:
            ended = authority.release_lease(
                "controller",
                nonce=lease.nonce,
                reason="retired",
            )
            assert ended.committed and ended.value is not None
            wake.publish_lease_generation_event(
                root,
                controller_label=ended.value.label,
                lease_nonce=ended.value.nonce,
                generation=ended.value.generation,
                state=ended.value.state,
            )

    monkeypatch.setattr(sessions.time, "sleep", tick)
    assert sessions.hold_controller_lock(
        root,
        label="controller",
        nonce=lease.nonce,
        pid=71001,
        start_token="cheap-tick",
    ) == 0
    assert sleeps["count"] == 2
    assert len(open_calls) == 2
    assert registration.closed is True


def test_lock_holder_keeps_witness_after_inconclusive_identity_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = _root(monkeypatch, tmp_path)
    identity = compat.process_start_identity(os.getpid())
    assert identity is not None
    expected = {
        "pid": os.getpid(),
        "start_token": str(identity["start_token"]),
    }
    authority = journal.open_or_create_journal(root)
    claimed = authority.claim_or_renew_lease(
        "controller",
        principal=expected,
    )
    assert claimed.committed and claimed.value is not None
    lease = claimed.value
    wake.publish_lease_generation_event(
        root,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        generation=lease.generation,
        state=lease.state,
    )

    monkeypatch.setattr(sessions, "_controller_process_identity", lambda _pid: expected)
    probe_results = iter((None, False))
    monkeypatch.setattr(
        compat,
        "process_identity_matches",
        lambda *_args: next(probe_results),
    )
    event_reads = 0
    original_read_event = wake.read_lease_generation_event

    def read_event(*args, **kwargs):
        nonlocal event_reads
        event_reads += 1
        return original_read_event(*args, **kwargs)

    monkeypatch.setattr(wake, "read_lease_generation_event", read_event)
    monkeypatch.setattr(sessions.time, "sleep", lambda _seconds: None)

    assert sessions.hold_controller_lock(
        root,
        label=lease.label,
        nonce=lease.nonce,
        pid=expected["pid"],
        start_token=expected["start_token"],
    ) == 0
    assert event_reads == 1


def test_same_measured_process_generation_renews_idempotently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _root(monkeypatch, tmp_path)
    monkeypatch.setattr(
        sessions,
        "_controller_process_identity",
        lambda pid: {"pid": pid, "start_token": "same-generation"},
    )
    first = sessions.claim_controller_startup(
        root, pid=71001, label="controller", role="controller"
    )
    second = sessions.claim_controller_startup(
        root, pid=71001, label="controller", role="controller"
    )
    assert first["claimed"] is True and second["claimed"] is True
    assert second["session"]["id"] == first["session"]["id"]
    assert second["session"]["generation"] == first["session"]["generation"]


def test_liveness_probes_keep_unknown_and_let_death_dominate() -> None:
    """UNKNOWN stays UNKNOWN; only a positive False probe proves death."""
    assert sessions._combine_liveness_probes(None, False) is False
    assert sessions._combine_liveness_probes(None, True) is None
    assert sessions._combine_liveness_probes(True, True, True) is True
    assert sessions._combine_liveness_probes(True, None, True) is None
    assert sessions._combine_liveness_probes(True, True, False) is False
    assert sessions._combine_liveness_probes(False, None, True) is False


def test_lease_liveness_uses_only_lock_when_process_identity_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _root(monkeypatch, tmp_path)
    authority = journal.open_or_create_journal(root)
    claimed = authority.claim_or_renew_lease(
        "controller",
        principal={"pid": 71001, "start_token": "audit-only"},
    )
    assert claimed.committed and claimed.value is not None
    holder = wake.register_lease_holder(
        root,
        controller_label="controller",
        lease_nonce=claimed.value.nonce,
    )
    monkeypatch.setattr(
        sessions,
        "_controller_process_identity",
        lambda _pid: (_ for _ in ()).throw(AssertionError("PID identity is not liveness")),
    )
    live = sessions._lease_holder_liveness(claimed.value)
    assert live is not None and live.alive is True
    holder.close()
    dead = sessions._lease_holder_liveness(claimed.value)
    assert dead is not None and dead.alive is False


def test_reused_pid_cannot_replace_active_claim_with_missing_lock_witness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing generation-lock is UNKNOWN, not proof the incumbent is dead."""
    root = _root(monkeypatch, tmp_path)
    host = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        live_identity = compat.process_start_identity(host.pid)
        assert live_identity is not None
        monkeypatch.setattr(
            sessions,
            "_controller_process_identity",
            lambda pid: (
                live_identity
                if pid == host.pid
                else {"pid": pid, "start_token": "reused-generation"}
            ),
        )
        first = sessions.claim_session(root, pid=host.pid, label="controller")
        assert wake.lease_holder_alive(
            root,
            controller_label="controller",
            lease_nonce=first["lease_nonce"],
        ) is None
        pid_calls: list[int] = []
        original_pid_liveness = compat.pid_liveness

        def pid_tripwire(pid: int) -> bool | None:
            pid_calls.append(pid)
            return original_pid_liveness(pid)

        monkeypatch.setattr(compat, "pid_liveness", pid_tripwire)
        result = sessions.claim_controller_startup(
            root,
            pid=71001,
            label="controller",
            role="controller",
            session_id=first["id"],
        )
        assert host.pid in pid_calls
        assert result["claimed"] is False
        assert result["reason"] == "label_in_use"
        active = journal.Journal(root).active_lease("controller")
        assert active is not None and active.generation == first["generation"]

        takeover = sessions.claim_controller_startup(
            root,
            pid=71001,
            label="controller",
            role="controller",
            session_id=first["id"],
            takeover=True,
        )
        assert takeover["claimed"] is True
        assert takeover["session"]["generation"] == first["generation"] + 1
        assert takeover["session"]["id"] != first["id"]
        ended = next(
            row
            for row in journal.Journal(root).lease_records(include_ended=True)
            if row["generation"] == first["generation"]
        )
        assert ended["state"] == "SUPERSEDED"
        assert ended["ended_reason"] == "explicit-takeover"
    finally:
        host.kill()
        host.wait(timeout=3)


def test_reused_pid_reconnects_without_takeover_when_identity_no_longer_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _root(monkeypatch, tmp_path)
    tokens = {71001: "first-generation"}
    monkeypatch.setattr(
        sessions,
        "_controller_process_identity",
        lambda pid: {"pid": pid, "start_token": tokens[pid]},
    )
    first = sessions.claim_session(root, pid=71001, label="controller")
    holder = wake.register_lease_holder(
        root,
        controller_label="controller",
        lease_nonce=first["lease_nonce"],
    )
    identity_checks: list[tuple[int, str]] = []
    monkeypatch.setattr(compat, "pid_liveness", lambda _pid: True)

    def identity_mismatch(pid: int, start_token: str) -> bool:
        identity_checks.append((pid, start_token))
        return False

    monkeypatch.setattr(compat, "process_identity_matches", identity_mismatch)
    tokens[71001] = "reused-generation"
    try:
        result = sessions.claim_controller_startup(
            root,
            pid=71001,
            label="controller",
            role="controller",
            session_id=first["id"],
        )
    finally:
        holder.close()
    assert result["claimed"] is True
    assert identity_checks == [(71001, "first-generation")]
    assert result["session"]["generation"] == first["generation"] + 1
    assert result["session"]["id"] != first["id"]
    ended = next(
        row
        for row in journal.Journal(root).lease_records(include_ended=True)
        if row["generation"] == first["generation"]
    )
    assert ended["state"] == "EXPIRED"
    assert ended["ended_reason"] == "holder-dead"


def test_stranger_cannot_take_label_when_pid_liveness_is_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _root(monkeypatch, tmp_path)
    host = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    original_kill = compat.os.kill
    holder = None
    try:
        identity = compat.process_start_identity(host.pid)
        assert identity is not None
        authority = journal.open_or_create_journal(root)
        claimed = authority.claim_or_renew_lease("alpha", principal=identity)
        assert claimed.committed and claimed.value is not None
        holder = wake.register_lease_holder(
            root,
            controller_label="alpha",
            lease_nonce=claimed.value.nonce,
        )

        def deny_pid_probe(pid: int, signal: int) -> None:
            if pid == host.pid:
                raise PermissionError(errno.EPERM, "Operation not permitted")
            original_kill(pid, signal)

        monkeypatch.setattr(compat.os, "kill", deny_pid_probe)
        assert compat.pid_liveness(host.pid) is None
        monkeypatch.setattr(
            sessions,
            "_controller_process_identity",
            lambda pid: {"pid": pid, "start_token": "stranger"},
        )
        refused = sessions.claim_controller_startup(
            root, pid=71931, label="alpha", role="controller"
        )
        assert refused["claimed"] is False
        assert refused["reason"] == "label_in_use"
        active = journal.Journal(root).active_lease("alpha")
        assert active is not None and active.generation == claimed.value.generation
    finally:
        monkeypatch.setattr(compat.os, "kill", original_kill)
        if holder is not None:
            holder.close()
        host.kill()
        host.wait(timeout=3)


def test_stranger_cannot_take_label_when_process_identity_is_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _root(monkeypatch, tmp_path)
    host = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    holder = None
    try:
        identity = compat.process_start_identity(host.pid)
        assert identity is not None
        authority = journal.open_or_create_journal(root)
        claimed = authority.claim_or_renew_lease("alpha", principal=identity)
        assert claimed.committed and claimed.value is not None
        holder = wake.register_lease_holder(
            root,
            controller_label="alpha",
            lease_nonce=claimed.value.nonce,
        )
        original_start_identity = compat.process_start_identity

        def unavailable_identity(pid: int, *, include_ancestry: bool = False):
            if pid == host.pid:
                return None
            return original_start_identity(pid, include_ancestry=include_ancestry)

        monkeypatch.setattr(compat, "process_start_identity", unavailable_identity)
        assert compat.pid_liveness(host.pid) is True
        assert (
            compat.process_identity_matches(host.pid, str(identity["start_token"]))
            is None
        )
        monkeypatch.setattr(
            sessions,
            "_controller_process_identity",
            lambda pid: {"pid": pid, "start_token": "stranger"},
        )
        refused = sessions.claim_controller_startup(
            root, pid=71932, label="alpha", role="controller"
        )
        assert refused["claimed"] is False
        assert refused["reason"] == "label_in_use"
        active = journal.Journal(root).active_lease("alpha")
        assert active is not None and active.generation == claimed.value.generation
    finally:
        if holder is not None:
            holder.close()
        host.kill()
        host.wait(timeout=3)


@pytest.mark.parametrize("unknown_probe", ("lock", "pid", "identity"))
def test_ancestry_matched_return_reconnects_when_probe_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    unknown_probe: str,
) -> None:
    root = _root(monkeypatch, tmp_path)
    holder_pid = os.getpid()
    holder_identity = compat.process_start_identity(holder_pid)
    assert holder_identity is not None
    identity = {
        "pid": holder_pid,
        "start_token": str(holder_identity["start_token"]),
    }
    returning = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    assert any(
        process.get("pid") == holder_pid
        and process.get("start_token") == identity["start_token"]
        for process in sessions._controller_process_ancestry(returning.pid)
    )
    original_kill = compat.os.kill
    holder = None
    try:
        authority = journal.open_or_create_journal(root)
        claimed = authority.claim_or_renew_lease("alpha", principal=identity)
        assert claimed.committed and claimed.value is not None
        if unknown_probe != "lock":
            holder = wake.register_lease_holder(
                root,
                controller_label="alpha",
                lease_nonce=claimed.value.nonce,
            )
        if unknown_probe == "pid":

            def deny_pid_probe(pid: int, signal: int) -> None:
                if pid == holder_pid:
                    raise PermissionError(errno.EPERM, "Operation not permitted")
                original_kill(pid, signal)

            monkeypatch.setattr(compat.os, "kill", deny_pid_probe)
        elif unknown_probe == "identity":
            original_start_identity = compat.process_start_identity

            def unavailable_identity(pid: int, *, include_ancestry: bool = False):
                if pid == holder_pid and not include_ancestry:
                    return None
                return original_start_identity(pid, include_ancestry=include_ancestry)

            monkeypatch.setattr(compat, "process_start_identity", unavailable_identity)

        assert sessions._incumbent_liveness_state(claimed.value) is None
        returned = sessions.claim_controller_startup(
            root,
            pid=returning.pid,
            label="alpha",
            role="controller",
            hold_lock=True,
        )
        assert returned["claimed"] is True
        assert returned["session"]["generation"] == claimed.value.generation
        assert returned["session"]["pid"] == holder_pid
        assert returned["session"]["kernel_lock_held"] is True
        if unknown_probe == "lock":
            released = sessions.release_session(root, pid=holder_pid)
            assert released["released"] is True
            deadline = time.monotonic() + 7
            while (
                wake.lease_holder_alive(
                    root,
                    controller_label="alpha",
                    lease_nonce=claimed.value.nonce,
                )
                is True
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            assert wake.lease_holder_alive(
                root,
                controller_label="alpha",
                lease_nonce=claimed.value.nonce,
            ) is not True
    finally:
        monkeypatch.setattr(compat.os, "kill", original_kill)
        if holder is not None:
            holder.close()
        returning.kill()
        returning.wait(timeout=3)


def test_name_only_reconnect_discards_historical_ambient_nonce(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A nonce from N-1 cannot collide while replacing generation N."""
    root = _root(monkeypatch, tmp_path)
    identities = {
        71001: {"pid": 71001, "start_token": "returning"},
        71002: {"pid": 71002, "start_token": "incumbent"},
    }
    monkeypatch.setattr(sessions, "_controller_process_identity", identities.get)
    first = sessions.claim_controller_startup(
        root, pid=71001, label="alpha", role="controller", environ={}
    )
    assert first["claimed"] is True
    first_holder = wake.register_lease_holder(
        root,
        controller_label="alpha",
        lease_nonce=first["session"]["lease_nonce"],
    )
    first_holder.close()
    second = sessions.claim_controller_startup(
        root, pid=71002, label="alpha", role="controller", environ={}
    )
    assert second["claimed"] is True
    second_holder = wake.register_lease_holder(
        root,
        controller_label="alpha",
        lease_nonce=second["session"]["lease_nonce"],
    )
    second_holder.close()

    result = sessions.claim_controller_startup(
        root,
        pid=71001,
        label="alpha",
        role="controller",
        environ={sessions.CONTROLLER_SESSION_ID_ENV: first["session"]["id"]},
    )

    assert result["claimed"] is True
    assert result["session"]["generation"] == second["session"]["generation"] + 1
    assert result["session"]["id"] not in {
        first["session"]["id"],
        second["session"]["id"],
    }
    rows = journal.Journal(root).lease_records(include_ended=True)
    assert len(rows) == 3
    assert len({row["nonce"] for row in rows}) == 3


def test_controller_startup_reconnects_dead_pid_lease_without_takeover(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A held lock reaches the real dead-PID probe before reconnecting."""
    root = _root(monkeypatch, tmp_path)
    dead_holder = subprocess.Popen([sys.executable, "-c", ""])
    dead_holder.wait(timeout=3)
    assert compat.pid_liveness(dead_holder.pid) is False
    identities = {
        dead_holder.pid: {"pid": dead_holder.pid, "start_token": "dead-holder"},
        71902: {"pid": 71902, "start_token": "returning"},
    }
    monkeypatch.setattr(sessions, "_controller_process_identity", identities.get)
    first = sessions.claim_controller_startup(
        root, pid=dead_holder.pid, label="alpha", role="controller"
    )
    assert first["claimed"] is True
    assert first["session"]["label"] == "alpha"
    holder = wake.register_lease_holder(
        root,
        controller_label="alpha",
        lease_nonce=first["session"]["lease_nonce"],
    )
    pid_calls: list[int] = []
    original_pid_liveness = compat.pid_liveness

    def pid_tripwire(pid: int) -> bool | None:
        pid_calls.append(pid)
        return original_pid_liveness(pid)

    monkeypatch.setattr(compat, "pid_liveness", pid_tripwire)
    try:
        second = sessions.claim_controller_startup(
            root, pid=71902, label="alpha", role="controller"
        )
    finally:
        holder.close()
    if dead_holder.pid not in pid_calls:
        raise AssertionError("pid_liveness was not consulted before reconnect")
    assert second["claimed"] is True
    assert second["session"]["label"] == "alpha"
    assert second["session"]["generation"] == first["session"]["generation"] + 1
    assert "takeover" not in str(second)
    ended = next(
        row
        for row in journal.Journal(root).lease_records(include_ended=True)
        if row["generation"] == first["session"]["generation"]
    )
    assert ended["state"] == "EXPIRED"
    assert ended["ended_reason"] == "holder-dead"


def test_controller_startup_reconnects_dead_pid_with_missing_lock_without_takeover(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """UNKNOWN lock must not hide a proven-dead PID or stall name-slug reconnect."""
    root = _root(monkeypatch, tmp_path)
    dead_holder = subprocess.Popen([sys.executable, "-c", ""])
    dead_holder.wait(timeout=3)
    assert compat.pid_liveness(dead_holder.pid) is False
    identities = {
        dead_holder.pid: {"pid": dead_holder.pid, "start_token": "dead-no-lock"},
        71904: {"pid": 71904, "start_token": "returning-no-lock"},
    }
    monkeypatch.setattr(sessions, "_controller_process_identity", identities.get)
    first = sessions.claim_controller_startup(
        root, pid=dead_holder.pid, label="alpha", role="controller"
    )
    assert first["claimed"] is True
    assert wake.lease_holder_alive(
        root,
        controller_label="alpha",
        lease_nonce=first["session"]["lease_nonce"],
    ) is None
    pid_calls: list[int] = []
    original_pid_liveness = compat.pid_liveness

    def pid_tripwire(pid: int) -> bool | None:
        pid_calls.append(pid)
        return original_pid_liveness(pid)

    monkeypatch.setattr(compat, "pid_liveness", pid_tripwire)
    second = sessions.claim_controller_startup(
        root, pid=71904, label="alpha", role="controller"
    )
    assert dead_holder.pid in pid_calls
    assert second["claimed"] is True
    assert second["session"]["generation"] == first["session"]["generation"] + 1
    assert "takeover" not in str(second)
    ended = next(
        row
        for row in journal.Journal(root).lease_records(include_ended=True)
        if row["generation"] == first["session"]["generation"]
    )
    assert ended["state"] == "EXPIRED"
    assert ended["ended_reason"] == "holder-dead"


def test_controller_startup_reconnects_when_lock_is_held_but_pid_is_dead(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _root(monkeypatch, tmp_path)
    identities = {
        71911: {"pid": 71911, "start_token": "dead-with-lock"},
        71912: {"pid": 71912, "start_token": "returning-with-lock"},
    }
    monkeypatch.setattr(sessions, "_controller_process_identity", identities.get)
    first = sessions.claim_controller_startup(
        root, pid=71911, label="alpha", role="controller"
    )
    assert first["claimed"] is True
    holder = wake.register_lease_holder(
        root,
        controller_label="alpha",
        lease_nonce=first["session"]["lease_nonce"],
    )
    try:
        second = sessions.claim_controller_startup(
            root, pid=71912, label="alpha", role="controller"
        )
    finally:
        holder.close()
    assert second["claimed"] is True
    assert second["session"]["generation"] == first["session"]["generation"] + 1


def test_stale_child_is_fenced_by_generation_not_nonce() -> None:
    coverage = {
        "coverage_id": "cov-1",
        "state": journal.COVERAGE_ARMED,
        "parent_pid": 41001,
        "lease_generation": 1,
        "lease_nonce": "same-record-nonce",
    }
    newer = {
        "state": journal.LEASE_ACTIVE,
        "generation": 2,
        "nonce": "same-record-nonce",
    }
    assert (
        journal.listener_exit_reason(
            coverage,
            newer,
            current_parent_pid=41001,
            identity_matches=True,
        )
        == "superseded"
    )
    same_generation = {
        "state": journal.LEASE_ACTIVE,
        "generation": 1,
        "nonce": "different-record-nonce",
    }
    assert (
        journal.listener_exit_reason(
            coverage,
            same_generation,
            current_parent_pid=41001,
            identity_matches=True,
        )
        is None
    )


def test_live_different_session_still_requires_takeover(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _root(monkeypatch, tmp_path)
    host = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        live_identity = sessions._controller_process_identity(host.pid)
        assert live_identity is not None
        identities = {
            host.pid: live_identity,
            71922: {"pid": 71922, "start_token": "contender"},
        }
        monkeypatch.setattr(sessions, "_controller_process_identity", identities.get)
        first = sessions.claim_controller_startup(
            root, pid=host.pid, label="alpha", role="controller"
        )
        assert first["claimed"] is True
        holder = wake.register_lease_holder(
            root,
            controller_label="alpha",
            lease_nonce=first["session"]["lease_nonce"],
        )
        try:
            refused = sessions.claim_controller_startup(
                root, pid=71922, label="alpha", role="controller"
            )
            assert refused["claimed"] is False
            assert refused["reason"] == "label_in_use"
            assert "takeover" in str(refused.get("message") or "")
            takeover = sessions.claim_controller_startup(
                root,
                pid=71922,
                label="alpha",
                role="controller",
                takeover=True,
            )
        finally:
            holder.close()
        assert takeover["claimed"] is True
        assert takeover["session"]["generation"] == first["session"]["generation"] + 1
    finally:
        host.kill()
        host.wait(timeout=3)


def test_release_requires_exact_process_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _root(monkeypatch, tmp_path)
    token = {"value": "generation-a"}
    monkeypatch.setattr(
        sessions,
        "_controller_process_identity",
        lambda pid: {"pid": pid, "start_token": token["value"]},
    )
    sessions.claim_session(root, pid=71001, label="controller")
    token["value"] = "generation-b"
    assert sessions.release_session(root, pid=71001)["released"] is False
    token["value"] = "generation-a"
    assert sessions.release_session(root, pid=71001)["released"] is True
    assert journal.Journal(root).active_lease("controller") is None
