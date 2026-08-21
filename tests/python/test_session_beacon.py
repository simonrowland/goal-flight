"""PID/start-token principals are verified within journal lease generations."""

from __future__ import annotations

import json
import os
from pathlib import Path
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


def _root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE_DIR", str(tmp_path / "task-store"))
    monkeypatch.setenv("GOALFLIGHT_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("GOALFLIGHT_WAKE_LEDGER_DIR", str(tmp_path / "wake-ledger"))
    monkeypatch.setenv("GOAL_FLIGHT_PIDFILE_DIR", str(tmp_path / "pidfiles"))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", "/dev/null")
    root = tmp_path / "project"
    root.mkdir()
    return root


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
    root = _root(monkeypatch, tmp_path)
    tokens = {71001: "first-generation"}
    monkeypatch.setattr(
        sessions,
        "_controller_process_identity",
        lambda pid: {"pid": pid, "start_token": tokens[pid]},
    )
    first = sessions.claim_session(root, pid=71001, label="controller")
    tokens[71001] = "reused-generation"
    result = sessions.claim_controller_startup(
        root,
        pid=71001,
        label="controller",
        role="controller",
        session_id=first["id"],
    )
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
    assert sessions.release_session(root, pid=71001) is False
    token["value"] = "generation-a"
    assert sessions.release_session(root, pid=71001) is True
    assert journal.Journal(root).active_lease("controller") is None
