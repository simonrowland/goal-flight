from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import socket
import sys
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_compat
import goalflight_journal as journal
import goalflight_liveness as liveness
import goalflight_messages as messages
import goalflight_session_status as session_status
import goalflight_watch as watch


def test_wall_jump_with_frozen_awake_clock_grants_local_wake_grace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(liveness.sys, "platform", "linux")
    wall = [1_000.0]
    awake = [100.0]
    monkeypatch.setattr(liveness, "_HOST_SLEEP_SAMPLE", None)
    monkeypatch.setattr(liveness, "_HOST_SLEEP_DETECTED_AT", None)
    monkeypatch.setattr(liveness, "_TEST_ACTIVE_TIME_SCALE_CHECKED", True)
    monkeypatch.setattr(liveness.time, "time", lambda: wall[0])
    monkeypatch.setattr(liveness.time, "clock_gettime", lambda _clock: awake[0])

    liveness.active_monotonic()
    wall[0] += 3600
    liveness.active_monotonic()

    assert liveness.host_slept_recently(now=wall[0])
    assert not liveness.host_slept_recently(now=wall[0] + liveness.HOST_SLEEP_GRACE_S + 1)


def test_darwin_kernel_recent_wake_grants_protection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 2_000_000_000.0
    monkeypatch.setattr(liveness.sys, "platform", "darwin")
    monkeypatch.setattr(liveness, "_HOST_WAKE_CACHE", None)
    monkeypatch.setattr(
        liveness, "_read_darwin_sleep_wake_times", lambda: (now - 3600, now - 30)
    )

    assert liveness.host_slept_recently(now=now, grace_s=300)


def test_darwin_kernel_old_wake_leaves_normal_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 2_000_000_000.0
    monkeypatch.setattr(liveness.sys, "platform", "darwin")
    monkeypatch.setattr(liveness, "_HOST_WAKE_CACHE", None)
    monkeypatch.setattr(
        liveness, "_read_darwin_sleep_wake_times", lambda: (now - 7200, now - 3600)
    )

    assert not liveness.host_slept_recently(now=now, grace_s=300)


def test_trace_activity_clock_excludes_sleep_and_keeps_awake_budget(
    tmp_path: Path,
) -> None:
    now = time.time()
    home = tmp_path / "home"
    cwd = "/Users/x/Repos/proj/worktrees/label/s-1"
    trace_dir = (
        home / ".cursor" / "projects" / cwd.lstrip("/").replace("/", "-")
        / "agent-transcripts" / "session-1"
    )
    trace_dir.mkdir(parents=True)
    trace = trace_dir / "trace.jsonl"
    trace.write_text("activity\n", encoding="utf-8")
    old = now - 30
    os.utime(trace, (old, old))
    tracker = watch.TraceLiveness(
        dispatch_id=None,
        worker_pid=999999,
        engine="cursor",
        engine_session_id="session-1",
        worker_cwd=cwd,
        home=home,
        started_epoch=now - 60,
        started_mono=100.0,
    )

    before_sleep = tracker.sample(
        now_epoch=now, now_mono=100.0, idle_threshold=900.0
    )
    after_sleep = tracker.sample(
        now_epoch=now + 3600, now_mono=100.0, idle_threshold=900.0
    )
    five_minutes_awake = tracker.sample(
        now_epoch=now + 3900, now_mono=400.0, idle_threshold=900.0
    )

    assert before_sleep["trace_active"] is True
    assert after_sleep["trace_active"] is True
    assert five_minutes_awake["trace_active"] is True


def test_trace_idle_for_hours_stays_idle_across_sleep(tmp_path: Path) -> None:
    now = time.time()
    home = tmp_path / "home"
    cwd = "/Users/x/Repos/proj/worktrees/label/s-1"
    trace_dir = (
        home / ".cursor" / "projects" / cwd.lstrip("/").replace("/", "-")
        / "agent-transcripts" / "session-1"
    )
    trace_dir.mkdir(parents=True)
    trace = trace_dir / "trace.jsonl"
    trace.write_text("activity\n", encoding="utf-8")
    old = now - 3600
    os.utime(trace, (old, old))
    tracker = watch.TraceLiveness(
        dispatch_id=None,
        worker_pid=999999,
        engine="cursor",
        engine_session_id="session-1",
        worker_cwd=cwd,
        home=home,
        started_epoch=now - 7200,
        started_mono=100.0,
    )

    before_sleep = tracker.sample(
        now_epoch=now, now_mono=3700.0, idle_threshold=900.0
    )
    after_sleep = tracker.sample(
        now_epoch=now + 3600, now_mono=3700.0, idle_threshold=900.0
    )
    five_minutes_awake = tracker.sample(
        now_epoch=now + 3900, now_mono=4000.0, idle_threshold=900.0
    )

    assert before_sleep["trace_active"] is False
    assert after_sleep["trace_active"] is False
    assert five_minutes_awake["trace_active"] is False


def test_stale_active_queue_is_abandoned_even_after_detected_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc).timestamp()
    touched = (datetime.fromtimestamp(now, timezone.utc) - timedelta(days=8)).isoformat()
    front = {"state": "active", "last-touched": touched}
    monkeypatch.setattr(liveness.sys, "platform", "darwin")
    monkeypatch.setattr(liveness, "_HOST_WAKE_CACHE", None)
    monkeypatch.setattr(
        liveness,
        "_read_darwin_sleep_wake_times",
        lambda: (now - 3600, now - 30),
    )

    assert liveness.host_slept_recently(now=now, grace_s=300)
    assert session_status.queue_active_now(front)[0] is False


def test_overdue_lease_after_sleep_reprobes_holder_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lease = SimpleNamespace(
        nonce="lease-nonce",
        renew_deadline_at="2000-01-01T00:00:00+00:00",
        label="controller",
        project_root=str(tmp_path),
    )
    probe = {
        "classification": messages.goalflight_wake._SUPERVISE_ARGV_MATCH,
        "lease_nonces": ["lease-nonce"],
    }
    monkeypatch.setattr(messages, "host_slept_recently", lambda: True)
    monkeypatch.setattr(messages.goalflight_wake, "lease_holder_alive", lambda *_a, **_k: True)
    live = messages._monitor_lease_status(probe, active_lease=lease)
    assert live == {"state": "current", "reason": "live-holder-after-host-sleep"}

    monkeypatch.setattr(messages.goalflight_wake, "lease_holder_alive", lambda *_a, **_k: False)
    dead = messages._monitor_lease_status(probe, active_lease=lease)
    assert dead["state"] == "orphaned"
    assert dead["reason"] == "renew-deadline-past"


@pytest.mark.parametrize("identity_alive,expired", [(True, False), (False, True)])
def test_expired_lease_sweep_rechecks_identity_after_sleep(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    identity_alive: bool,
    expired: bool,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    authority = journal.Journal.create(project)
    identity = goalflight_compat.process_start_identity(os.getpid())
    assert identity is not None
    claimed = authority.claim_or_renew_lease(
        "controller",
        principal={
            "pid": os.getpid(),
            "start_token": str(identity["start_token"]),
            "hostname": socket.gethostname(),
        },
    )
    assert claimed.committed
    monkeypatch.setattr(journal, "host_slept_recently", lambda: True)
    monkeypatch.setattr(journal.goalflight_wake, "lease_holder_alive", lambda *_a, **_k: False)
    monkeypatch.setattr(
        journal.goalflight_compat,
        "process_identity_matches",
        lambda *_a, **_k: identity_alive,
    )

    result = authority.expire_stale_leases()

    assert bool(result.value) is expired
    assert (authority.active_lease("controller") is None) is expired
