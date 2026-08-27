"""Focused tests for capacity TTL-prune liveness gate + non-mutating status.

Covers three fixes (B-P0, B-P1, B-P2) in scripts/goalflight_capacity.py:

  B-P0  prune_state must NOT TTL-expire a lease with any live holder pid.
        capacity.json is shared across sibling projects under one
        /tmp/goal-flight-<uid>/ dir; a clock-only TTL eviction here would
        over-subscribe the machine while a sibling project's worker is still
        consuming RAM. Only a lease with no live worker, controller, or
        pre-attach claimant is reclaimable by TTL.

  B-P1  cmd_status is a READ. It must compute a pruned VIEW without persisting,
        so a frequent status poll can't race-evict another project's live lease.

  B-P2  an unowned acquire keeps controller ownership null while a separate
        claimant pid protects the lease until the worker pid is attached.

State is isolated via $GOALFLIGHT_STATE_DIR (read at call time by state_dir()),
so these tests never read or mutate the real shared capacity.json.
"""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows(
    "capacity liveness tests spawn a real subprocess and probe pid liveness"
)

import datetime as dt
import argparse
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import goalflight_capacity as cap  # noqa: E402
import goalflight_compat as compat  # noqa: E402
import goalflight_status as status  # noqa: E402


def _kill_if_alive(pid: int | None) -> None:
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _spawn_live_worker() -> subprocess.Popen:
    """A real, long-lived child whose pid is genuinely alive for the test."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _dead_pid() -> int:
    """Spawn a child, reap it, and return its now-dead pid."""
    proc = subprocess.Popen(
        [sys.executable, "-c", ""],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.wait()
    # The pid is reaped; pid_alive() reports it dead. (Reuse within the short
    # test window is not a concern for these assertions.)
    assert not cap.pid_alive(proc.pid), "freshly reaped pid should read as dead"
    return proc.pid


def _indeterminate_foreign_pid() -> int:
    """A pid whose probe is the real non-ESRCH failure this host produces.

    Non-root Darwin: os.kill(1, 0) raises PermissionError / EPERM, so
    pid_liveness(1) is None while pid_alive(1) stays True. Signal 0 only —
    this does not kill or signal pid 1. Fail loudly if this host cannot
    induce the condition; a patched OSError would not exercise F1.
    """
    pid = 1
    live = compat.pid_liveness(pid)
    assert live is None, (
        f"pid {pid} liveness is {live!r}; need a real indeterminate (EPERM) "
        "probe to exercise bounded reclaim"
    )
    assert cap.pid_alive(pid) is True, (
        "boolean pid_alive must still treat indeterminate as live (kill/reap)"
    )
    return pid


def _past_ttl_lease(*, worker_pid: int | None, controller_pid: int | None) -> dict:
    """Build an ACTIVE lease whose expires_at is far in the past."""
    expired = cap.iso(cap.utc_now() - dt.timedelta(hours=12))
    return {
        "lease_id": f"lease-{worker_pid}-{controller_pid}",
        "agent": "codex",
        "state": "active",
        "worker_pid": worker_pid,
        "controller_pid": controller_pid,
        "mem_mb": 386,
        "started_at": cap.iso(cap.utc_now() - dt.timedelta(hours=20)),
        "expires_at": expired,
    }


def _future_active_lease(idx: int, *, agent: str = "codex") -> dict:
    now = cap.utc_now()
    return {
        "lease_id": f"adaptive-held-{idx}",
        "dispatch_id": f"adaptive-held-{idx}",
        "agent": agent,
        "state": "active",
        "worker_pid": None,
        "controller_pid": os.getpid(),
        "mem_mb": 386,
        "started_at": cap.iso(now),
        "expires_at": cap.iso(now + dt.timedelta(hours=1)),
    }


def _seed_codex_at_capacity_records(state_dir: Path, *, count: int = 3) -> None:
    runs = state_dir / "runs.d"
    statuses = state_dir / "dispatch"
    runs.mkdir(parents=True, exist_ok=True)
    statuses.mkdir(parents=True, exist_ok=True)
    recent_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    for idx in range(count):
        status_path = statuses / f"codex-capacity-{idx}.status.json"
        status_path.write_text(
            json.dumps(
                {
                    "schema": "goalflight.status.v1",
                    "dispatch_id": f"codex-capacity-{idx}",
                    "agent": "codex",
                    "state": "worker_dead",
                    "error": "ERROR: Selected model is at capacity. Please try a different model.",
                },
                sort_keys=True,
            )
        )
        (runs / f"codex-capacity-{idx}.json").write_text(
            json.dumps(
                {
                    "schema": "goalflight.dispatch.v1",
                    "dispatch_id": f"codex-capacity-{idx}",
                    "agent": "codex",
                    "state": "failed",
                    "started_at": recent_iso,
                    "updated_at": recent_iso,
                    "status_path": str(status_path),
                },
                sort_keys=True,
            )
        )


def case_live_worker_past_ttl_survives_prune() -> None:
    """A LIVE worker past its TTL must NOT be flipped to expired (B-P0)."""
    worker = _spawn_live_worker()
    try:
        lease = _past_ttl_lease(worker_pid=worker.pid, controller_pid=os.getpid())
        data = {"leases": {lease["lease_id"]: lease}, "cooldowns": {}}
        cap.prune_state(data)
        survived = data["leases"].get(lease["lease_id"])
        assert survived is not None, "live past-TTL lease was removed entirely"
        assert survived["state"] == "active", (
            f"live past-TTL lease was TTL-expired (state={survived['state']!r})"
        )
        # And it still counts toward active capacity (no over-subscription).
        assert len(cap.active_leases(data)) == 1
    finally:
        _kill_if_alive(worker.pid)
        worker.wait()


def case_live_worker_dead_controller_survives_prune() -> None:
    """Worker alive but orchestrator pid dead -> still LIVE, must survive (B-P0).

    Liveness is OR across the two pids: any live pid means the lease is still
    consuming RAM and is not a clock-only TTL eviction candidate.
    """
    worker = _spawn_live_worker()
    try:
        lease = _past_ttl_lease(worker_pid=worker.pid, controller_pid=_dead_pid())
        data = {"leases": {lease["lease_id"]: lease}, "cooldowns": {}}
        cap.prune_state(data)
        survived = data["leases"].get(lease["lease_id"])
        assert survived is not None and survived["state"] == "active", (
            "lease with a live worker (dead controller) was wrongly TTL-expired"
        )
    finally:
        _kill_if_alive(worker.pid)
        worker.wait()


def case_dead_lease_past_ttl_is_reclaimed() -> None:
    """Both worker + controller dead AND past TTL -> reclaimed, no leak (B-P0)."""
    lease = _past_ttl_lease(worker_pid=_dead_pid(), controller_pid=_dead_pid())
    data = {"leases": {lease["lease_id"]: lease}, "cooldowns": {}}
    cap.prune_state(data)
    survivor = data["leases"].get(lease["lease_id"])
    # Either flipped to a non-active terminal state or popped outright; in both
    # cases it no longer counts as an active (RAM-holding) lease.
    if survivor is not None:
        assert survivor["state"] == "expired", (
            f"dead past-TTL lease not expired (state={survivor['state']!r})"
        )
    assert cap.active_leases(data) == [], "dead past-TTL lease still counted active"


def case_dead_lease_no_ttl_not_expired() -> None:
    """A lease with dead pids but NOT past TTL is left alone by prune.

    prune_state only TTL-expires; reclaiming a not-yet-expired stale lease is the
    job of release-stale, not prune. Guards against an over-eager liveness sweep.
    """
    lease = _past_ttl_lease(worker_pid=_dead_pid(), controller_pid=_dead_pid())
    lease["expires_at"] = cap.iso(cap.utc_now() + dt.timedelta(hours=8))  # future
    data = {"leases": {lease["lease_id"]: lease}, "cooldowns": {}}
    cap.prune_state(data)
    survived = data["leases"].get(lease["lease_id"])
    assert survived is not None and survived["state"] == "active", (
        "prune wrongly expired a dead-but-not-yet-past-TTL lease"
    )


def case_rate_limited_retained_lease_pruned() -> None:
    """Retained (--keep) leases in canonical terminal-FAILURE states must prune.

    Regression: TERMINAL_LEASE_STATES was hand-maintained and had drifted from
    goalflight_dispatch_states.TERMINAL_FAILURE_STATES, so a retained lease in a
    newer state (rate_limited, stalled, remote_turn_silence, failed_worktree)
    was never popped by prune_state and accumulated forever in capacity.json.
    The set is now derived from the canonical vocabulary; assert membership and
    the actual prune behavior (the path TERMINAL_LEASE_STATES gates).
    """
    for state in ("rate_limited", "stalled", "remote_turn_silence", "failed_worktree"):
        assert state in cap.TERMINAL_LEASE_STATES, (
            f"{state!r} (a canonical terminal-failure state) missing from "
            "TERMINAL_LEASE_STATES -> retained leases in it would leak"
        )

    # >24h-old terminal rate_limited lease is pruned (pids irrelevant on the
    # terminal-state path; no expires_at so the TTL branch is skipped).
    old = {
        "lease_id": "rl-old",
        "agent": "codex",
        "state": "rate_limited",
        "worker_pid": None,
        "controller_pid": None,
        "ended_at": cap.iso(cap.utc_now() - dt.timedelta(hours=25)),
    }
    data = {"leases": {"rl-old": old}, "cooldowns": {}}
    cap.prune_state(data)
    assert "rl-old" not in data["leases"], (
        "a >24h-old retained rate_limited lease was not pruned"
    )

    # A terminal rate_limited lease with no terminal timestamp prunes immediately.
    no_ts = {
        "lease_id": "rl-no-ts",
        "agent": "codex",
        "state": "rate_limited",
        "worker_pid": None,
        "controller_pid": None,
    }
    data2 = {"leases": {"rl-no-ts": no_ts}, "cooldowns": {}}
    cap.prune_state(data2)
    assert "rl-no-ts" not in data2["leases"], (
        "a terminal rate_limited lease with no terminal_at was not pruned"
    )

    # A <24h-old terminal rate_limited lease stays (within the prune window).
    recent = {
        "lease_id": "rl-recent",
        "agent": "codex",
        "state": "rate_limited",
        "worker_pid": None,
        "controller_pid": None,
        "ended_at": cap.iso(cap.utc_now() - dt.timedelta(hours=1)),
    }
    data3 = {"leases": {"rl-recent": recent}, "cooldowns": {}}
    cap.prune_state(data3)
    assert "rl-recent" in data3["leases"], (
        "a <24h-old terminal rate_limited lease was pruned too early"
    )


def case_capacity_wait_resolution_precedence() -> None:
    assert cap.resolve_capacity_wait_s(lane="bulk", wait_s=7, env={"GOALFLIGHT_CAPACITY_WAIT_S": "3"}) == 7.0
    assert cap.resolve_capacity_wait_s(lane="critical", wait_s=None, env={"GOALFLIGHT_CAPACITY_WAIT_S": "4.5"}) == 4.5
    assert cap.resolve_capacity_wait_s(lane="critical", wait_s=None, env={}) == 120.0
    assert cap.resolve_capacity_wait_s(lane="bulk", wait_s=None, env={"GOALFLIGHT_CAPACITY_WAIT_S": "bad"}) == 900.0


def case_acquire_with_wait_zero_preserves_single_shot_payload() -> None:
    calls = []

    def fake_acquire(_args):
        calls.append("call")
        print(json.dumps({"decision": "wait", "reason": "machine_worker_cap"}))
        return 2

    payload = cap.acquire_with_wait(
        argparse.Namespace(),
        lane="normal",
        wait_s=0,
        poll_s=1,
        jitter=0,
        sleep_fn=lambda _s: (_ for _ in ()).throw(AssertionError("single-shot slept")),
        acquire_func=fake_acquire,
    )
    assert calls == ["call"], calls
    assert payload == {"decision": "wait", "reason": "machine_worker_cap"}, payload


def case_acquire_with_wait_jitter_bounds_and_deadline_math() -> None:
    clock = [100.0]
    calls: list[float] = []
    sleeps: list[float] = []
    waits: list[tuple[int, float, str]] = []

    def fake_acquire(_args):
        calls.append(clock[0])
        print(json.dumps({"decision": "wait", "reason": "machine_worker_cap"}))
        return 2

    def fake_sleep(duration: float) -> None:
        sleeps.append(round(duration, 3))
        clock[0] += duration

    def fake_random(low: float, high: float) -> float:
        assert low == 0.0 and high == 0.5
        return 0.5

    def on_wait(attempt: int, remaining_s: float, reason: dict) -> None:
        waits.append((attempt, round(remaining_s, 3), reason["reason"]))

    payload = cap.acquire_with_wait(
        argparse.Namespace(),
        lane="normal",
        wait_s=2.2,
        poll_s=1.0,
        jitter=0.5,
        on_wait=on_wait,
        monotonic_fn=lambda: clock[0],
        sleep_fn=fake_sleep,
        random_fn=fake_random,
        acquire_func=fake_acquire,
    )

    assert payload["decision"] == "wait", payload
    assert "attempts" not in payload and "waited_s" not in payload, payload
    assert len(calls) == 3, calls
    assert sleeps == [0.5, 0.5, 0.5, 0.5, 0.2], sleeps
    assert waits == [(1, 2.2, "machine_worker_cap"), (2, 0.7, "machine_worker_cap")], waits


def case_acquire_with_wait_signal_interrupts_sleep_promptly() -> None:
    clock = [500.0]
    sleeps: list[float] = []
    old_handler = signal.getsignal(signal.SIGTERM)

    def fake_acquire(_args):
        print(json.dumps({"decision": "wait", "reason": "machine_worker_cap"}))
        return 2

    def fake_sleep(duration: float) -> None:
        sleeps.append(duration)
        signal.raise_signal(signal.SIGTERM)
        clock[0] += duration

    try:
        try:
            cap.acquire_with_wait(
                argparse.Namespace(),
                lane="normal",
                wait_s=60.0,
                poll_s=15.0,
                jitter=0,
                install_signal_handlers=True,
                monotonic_fn=lambda: clock[0],
                sleep_fn=fake_sleep,
                acquire_func=fake_acquire,
            )
        except cap.CapacityWaitInterrupted as exc:
            payload = exc.payload
        else:
            raise AssertionError("SIGTERM during capacity wait did not interrupt")
    finally:
        assert signal.getsignal(signal.SIGTERM) == old_handler

    assert sleeps == [0.5], sleeps
    assert payload == {
        "decision": "wait",
        "reason": "wait_interrupted",
        "waited_s": 0.0,
        "attempts": 1,
    }, payload
    assert clock[0] == 500.0, clock


def case_status_is_non_mutating_for_live_lease(state_dir: Path) -> None:
    """`status` must not persist a prune that would evict a live lease (B-P1)."""
    worker = _spawn_live_worker()
    try:
        # Seed the shared (isolated) capacity.json with a LIVE past-TTL lease.
        lease = _past_ttl_lease(worker_pid=worker.pid, controller_pid=os.getpid())
        seed = {
            "schema": cap.SCHEMA,
            "machine_id": cap.machine_id(),
            "leases": {lease["lease_id"]: lease},
            "cooldowns": {},
        }
        cap.save_state(seed)
        before = cap.state_path().read_text()

        rc = cap.main(["status", "--json", "--ram-mb", "65536"])
        assert rc == 0, rc

        after = cap.state_path().read_text()
        # status no longer calls save_state at all -> the file is byte-identical.
        assert after == before, "status mutated/persisted shared capacity state"
        reloaded = json.loads(after)
        kept = reloaded["leases"].get(lease["lease_id"])
        assert kept is not None and kept["state"] == "active", (
            "status poll evicted/expired a live lease on disk (B-P1 regression)"
        )
    finally:
        _kill_if_alive(worker.pid)
        worker.wait()


def case_status_still_reclaims_dead_lease_in_view(state_dir: Path) -> None:
    """Status VIEW prunes a dead past-TTL lease for display (no disk write)."""
    lease = _past_ttl_lease(worker_pid=_dead_pid(), controller_pid=_dead_pid())
    seed = {
        "schema": cap.SCHEMA,
        "machine_id": cap.machine_id(),
        "leases": {lease["lease_id"]: lease},
        "cooldowns": {},
    }
    cap.save_state(seed)

    out = subprocess.check_output(
        [sys.executable, str(REPO_ROOT / "scripts" / "goalflight_capacity.py"),
         "status", "--json", "--ram-mb", "65536"],
        text=True,
        env={**os.environ, "GOALFLIGHT_STATE_DIR": str(state_dir)},
    )
    payload = json.loads(out)
    assert payload["active"] == [], (
        "status view still reported a dead past-TTL lease as active"
    )


def case_indeterminate_holder_bounded_not_indefinite() -> None:
    """F1: EPERM worker_pid is protected only inside INDETERMINATE_LIVE_RETENTION_S.

    Induces the real condition (worker_pid=1, expires_at in the past). Both
    halves: not reclaimed immediately, reclaimed after the bound. Boolean
    pid_alive stays True for the same pid; reclaim consults pid_liveness.
    """
    foreign = _indeterminate_foreign_pid()
    dead_controller = _dead_pid()
    now = cap.utc_now()

    recent = _past_ttl_lease(worker_pid=foreign, controller_pid=dead_controller)
    recent["started_at"] = cap.iso(now - dt.timedelta(seconds=30))
    recent["expires_at"] = cap.iso(now - dt.timedelta(seconds=1))
    recent_data = {"leases": {recent["lease_id"]: recent}, "cooldowns": {}}
    cap.prune_state(recent_data)
    kept = recent_data["leases"].get(recent["lease_id"])
    assert kept is not None and kept["state"] == "active", (
        f"indeterminate holder inside {cap.INDETERMINATE_LIVE_RETENTION_S}s "
        f"was reclaimed immediately (state={None if kept is None else kept.get('state')!r})"
    )
    assert recent not in cap.stale_active_leases(recent_data), (
        "indeterminate holder inside the retention window was classified stale"
    )
    assert len(cap.active_leases(recent_data)) == 1

    aged = _past_ttl_lease(worker_pid=foreign, controller_pid=dead_controller)
    aged["started_at"] = cap.iso(
        now - dt.timedelta(seconds=cap.INDETERMINATE_LIVE_RETENTION_S + 120)
    )
    aged["expires_at"] = cap.iso(
        now - dt.timedelta(seconds=cap.INDETERMINATE_LIVE_RETENTION_S + 60)
    )
    aged_for_stale = dict(aged)
    aged_data = {"leases": {aged["lease_id"]: aged}, "cooldowns": {}}
    assert aged_for_stale in cap.stale_active_leases(
        {"leases": {aged_for_stale["lease_id"]: aged_for_stale}, "cooldowns": {}}
    ), "indeterminate holder past the retention window was not classified stale"
    cap.prune_state(aged_data)
    survivor = aged_data["leases"].get(aged["lease_id"])
    if survivor is not None:
        assert survivor["state"] == "expired", (
            f"indeterminate holder past retention not expired "
            f"(state={survivor['state']!r})"
        )
    assert cap.active_leases(aged_data) == [], (
        "indeterminate holder past retention still counted active"
    )


def case_live_worker_survives_past_indeterminate_retention() -> None:
    """Confirmed-live worker is never reclaimed by the indeterminate bound."""
    worker = _spawn_live_worker()
    try:
        lease = _past_ttl_lease(worker_pid=worker.pid, controller_pid=_dead_pid())
        now = cap.utc_now()
        lease["expires_at"] = cap.iso(
            now - dt.timedelta(seconds=cap.INDETERMINATE_LIVE_RETENTION_S + 60)
        )
        data = {"leases": {lease["lease_id"]: lease}, "cooldowns": {}}
        cap.prune_state(data)
        survived = data["leases"].get(lease["lease_id"])
        assert survived is not None and survived["state"] == "active", (
            "confirmed-live worker was reclaimed after the indeterminate bound"
        )
        assert lease not in cap.stale_active_leases(data), (
            "confirmed-live worker was classified stale after the bound"
        )
    finally:
        _kill_if_alive(worker.pid)
        worker.wait()


def case_unprobeable_retained_scope_reclaims_after_until() -> None:
    """Watcher retain path: elapsed until + unprobeable pgid is reclaimable.

    ``accounted_live_pgid=1`` is not a stubbed None: ``_process_group_liveness``
    refuses pgid<=1. Combined with a past ``accounted_live_until``, the existing
    retain predicate must stop holding. A still-open until keeps the hold.
    Confirmed-live groups still hold after the same until (watch idle tests).
    """
    now = cap.utc_now()
    lease = {
        "lease_id": "retained-unprobeable",
        "agent": "codex",
        "state": "active",
        "reason": cap.INDETERMINATE_LIVE_REASON,
        "worker_pid": _indeterminate_foreign_pid(),
        "controller_pid": None,
        "accounted_live_pgid": 1,
        "accounted_live_until": "1970-01-01T00:00:00Z",
        "started_at": cap.iso(now - dt.timedelta(hours=20)),
        "expires_at": cap.iso(now - dt.timedelta(hours=12)),
    }
    assert not cap.retained_live_scope_holds_capacity(lease)
    data = {"leases": {lease["lease_id"]: lease}, "cooldowns": {}}
    assert lease in cap.stale_active_leases(data), lease
    cap.prune_state(data)
    survivor = data["leases"].get(lease["lease_id"])
    if survivor is not None:
        assert survivor["state"] == "expired", survivor
    assert cap.active_leases(data) == []

    still_open = dict(lease)
    still_open["state"] = "active"
    still_open["accounted_live_until"] = cap.iso(now + dt.timedelta(hours=1))
    assert cap.retained_live_scope_holds_capacity(still_open)
    open_data = {"leases": {still_open["lease_id"]: still_open}, "cooldowns": {}}
    assert still_open not in cap.stale_active_leases(open_data)
    cap.prune_state(open_data)
    assert open_data["leases"][still_open["lease_id"]]["state"] == "active"


def case_dead_worker_reclaimed_inside_indeterminate_retention() -> None:
    """ESRCH / confirmed-dead is still reclaimed promptly, not after 7200s."""
    lease = _past_ttl_lease(worker_pid=_dead_pid(), controller_pid=_dead_pid())
    now = cap.utc_now()
    lease["started_at"] = cap.iso(now - dt.timedelta(seconds=30))
    lease["expires_at"] = cap.iso(now - dt.timedelta(seconds=1))
    data = {"leases": {lease["lease_id"]: lease}, "cooldowns": {}}
    assert lease in cap.stale_active_leases(data), (
        "confirmed-dead worker inside the retention window was not stale"
    )
    cap.prune_state(data)
    survivor = data["leases"].get(lease["lease_id"])
    if survivor is not None:
        assert survivor["state"] == "expired", (
            f"confirmed-dead past-TTL lease not expired (state={survivor['state']!r})"
        )
    assert cap.active_leases(data) == [], (
        "confirmed-dead worker still counted active inside the retention window"
    )


def case_stale_active_leases_live_worker_not_stale_with_dead_controller() -> None:
    """Poison-pair: stale_active_leases must skip a lease whose worker_pid is LIVE.

    Regression guard (audit-r24-2 / D008): dead controller + live worker must not
    be classified stale; both pids dead must be. Dropping the worker_pid liveness
    gate would mark the live-worker lease stale and fail this test.
    """
    worker = _spawn_live_worker()
    try:
        live_worker_dead_controller = _past_ttl_lease(
            worker_pid=worker.pid,
            controller_pid=_dead_pid(),
        )
        both_dead = _past_ttl_lease(worker_pid=_dead_pid(), controller_pid=_dead_pid())
        data = {
            "leases": {
                live_worker_dead_controller["lease_id"]: live_worker_dead_controller,
                both_dead["lease_id"]: both_dead,
            },
            "cooldowns": {},
        }
        stale_ids = {lease["lease_id"] for lease in cap.stale_active_leases(data)}
        assert live_worker_dead_controller["lease_id"] not in stale_ids, (
            "live worker_pid was classified stale despite dead controller"
        )
        assert both_dead["lease_id"] in stale_ids, (
            "lease with both pids dead was not classified stale"
        )
    finally:
        _kill_if_alive(worker.pid)
        worker.wait()


def case_release_stale_poison_pair_live_worker_survives_dead_controller(state_dir: Path) -> None:
    """Poison-pair: no detach markers, dead controller, live worker stays held."""
    worker = _spawn_live_worker()
    lease_id = "poison-pair-live-worker-dead-controller"
    try:
        lease = _past_ttl_lease(worker_pid=worker.pid, controller_pid=_dead_pid())
        lease["lease_id"] = lease_id
        lease["expires_at"] = cap.iso(cap.utc_now() + dt.timedelta(hours=8))
        assert not any(key.startswith("detached_") for key in lease)
        cap.save_state(
            {
                "schema": cap.SCHEMA,
                "machine_id": cap.machine_id(),
                "leases": {lease_id: lease},
                "cooldowns": {},
            }
        )

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cap.main(["release-stale", "--reason", "poison_pair_test"])
        payload = json.loads(buf.getvalue())
        assert rc == 0, rc
        assert payload["released"] == [], payload
        data = json.loads(cap.state_path().read_text())
        assert data["leases"][lease_id]["state"] == "active", (
            "release-stale reclaimed a live worker keyed to a dead controller"
        )

        _kill_if_alive(worker.pid)
        worker.wait()
        assert not cap.pid_alive(worker.pid), "killed worker pid should read dead"

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cap.main(["release-stale", "--reason", "poison_pair_test"])
        payload = json.loads(buf.getvalue())
        assert rc == 0, rc
        assert payload["released"] == [lease_id], payload
        data = json.loads(cap.state_path().read_text())
        assert lease_id not in data["leases"], (
            "release-stale failed to reclaim the lease after worker death"
        )
    finally:
        _kill_if_alive(worker.pid)
        worker.wait()


def case_aggregate_status_payload_does_not_persist_prune(state_dir: Path) -> None:
    """goalflight_status.status_payload is a read path; --wait polls it often."""
    lease = _past_ttl_lease(worker_pid=_dead_pid(), controller_pid=_dead_pid())
    seed = {
        "schema": cap.SCHEMA,
        "machine_id": cap.machine_id(),
        "leases": {lease["lease_id"]: lease},
        "cooldowns": {},
    }
    cap.save_state(seed)
    before = cap.state_path().read_text()

    orig_dispatch_payload = status.goalflight_ledger.status_payload
    orig_rate_pressure = status.goalflight_capacity.current_rate_pressure
    try:
        status.goalflight_ledger.status_payload = lambda: {
            "schema": "goalflight.dispatch.v1",
            "records": [],
            "surplus_processes": [],
        }
        status.goalflight_capacity.current_rate_pressure = lambda args=None: None
        payload = status.status_payload()
    finally:
        status.goalflight_ledger.status_payload = orig_dispatch_payload
        status.goalflight_capacity.current_rate_pressure = orig_rate_pressure

    after = cap.state_path().read_text()
    assert after == before, "aggregate status_payload persisted a prune to capacity.json"
    view_lease = payload["capacity_state"]["leases"].get(lease["lease_id"])
    assert view_lease is not None and view_lease["state"] == "expired", (
        "aggregate status_payload should still return the pruned display view"
    )


def case_acquire_atomic_gate_still_blocks_over_cap(state_dir: Path) -> None:
    """cmd_acquire's check-then-act under StateLock still enforces the cap.

    Guards that the prune change did not regress the acquire gate: with the
    machine cap pinned to 1 and a live lease already held, a second acquire
    must be refused (decision=wait).
    """
    worker = _spawn_live_worker()
    try:
        rc1 = cap.main([
            "acquire", "--agent", "codex", "--worker-pid", str(worker.pid),
            "--max-total", "1", "--ram-mb", "65536", "--ttl-s", "3600",
            "--lease-id", "lease-hold",
        ])
        assert rc1 == 0, f"first acquire should be allowed (rc={rc1})"

        # Second acquire on a full machine -> wait (rc 2), no new lease.
        rc2 = cap.main([
            "acquire", "--agent", "codex", "--worker-pid", str(os.getpid()),
            "--max-total", "1", "--ram-mb", "65536", "--ttl-s", "3600",
            "--lease-id", "lease-second",
        ])
        assert rc2 == 2, f"second acquire over cap should wait (rc={rc2})"

        data = json.loads(cap.state_path().read_text())
        assert "lease-second" not in data["leases"], "over-cap lease was created"
        assert data["leases"]["lease-hold"]["state"] == "active"
    finally:
        _kill_if_alive(worker.pid)
        worker.wait()


def case_unowned_acquire_tracks_pre_attach_claimant(state_dir: Path) -> None:
    """A live launcher protects an unowned lease without becoming its owner."""
    cap.save_state(
        {
            "schema": cap.SCHEMA,
            "machine_id": cap.machine_id(),
            "leases": {},
            "cooldowns": {},
        }
    )
    output = io.StringIO()
    with redirect_stdout(output):
        rc = cap.main(
            [
                "acquire",
                "--agent",
                "codex",
                "--lease-id",
                "unowned-pre-attach",
                "--project-root",
                str(REPO_ROOT),
                "--ttl-s",
                "3600",
                "--ram-mb",
                "65536",
                "--max-total",
                "20",
            ]
        )
    assert rc == 0, (rc, output.getvalue())

    data = cap.load_state()
    lease = data["leases"]["unowned-pre-attach"]
    assert lease.get("controller_pid") is None, lease
    assert lease.get("worker_pid") is None, lease
    assert lease.get("claimant_pid") == os.getpid(), lease
    assert lease not in cap.stale_active_leases(data), lease

    lease["expires_at"] = cap.iso(cap.utc_now() - dt.timedelta(seconds=1))
    cap.prune_state(data)
    assert lease.get("state") == "active", lease

    lease["claimant_pid"] = _dead_pid()
    assert lease in cap.stale_active_leases(data), lease
    cap.prune_state(data)
    assert lease.get("state") == "expired", lease


def case_adaptive_rate_pressure_reduces_codex_effective_cap(state_dir: Path) -> None:
    """Clustered codex model-capacity failures halve only codex effective cap.

    Derived from DEFAULT_AGENT_CAPS (not hardcoded counts) so capacity tunes
    don't break the adaptive-halving contract this case actually tests.
    """
    base_cap = cap.DEFAULT_AGENT_CAPS["codex"]
    adapted_cap = max(1, base_cap // 2)
    _seed_codex_at_capacity_records(state_dir, count=3)
    leases = {
        _future_active_lease(idx)["lease_id"]: _future_active_lease(idx)
        for idx in range(adapted_cap)
    }
    cap.save_state(
        {
            "schema": cap.SCHEMA,
            "machine_id": cap.machine_id(),
            "leases": leases,
            "cooldowns": {},
        }
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cap.main([
            "acquire",
            "--agent", "codex",
            "--lease-id", "adaptive-sixth",
            "--project-root", str(REPO_ROOT),
            "--ttl-s", "3600",
            "--ram-mb", "65536",
            "--max-total", "20",
        ])
    assert rc == 2, f"adaptive over-cap codex acquire should wait (rc={rc}, stdout={buf.getvalue()})"
    payload = json.loads(buf.getvalue())
    assert payload["reason"] == "adaptive_rate_pressure", payload
    assert payload["active"] == adapted_cap, payload
    assert payload["base_agent_cap"] == base_cap, payload
    assert payload["agent_cap"] == adapted_cap, payload
    pressure = payload["adaptive_rate_pressure"]
    assert pressure["scope"] == "agent", pressure
    assert pressure["provider"] == "openai", pressure
    assert pressure["budget_key"] == "agent:codex", pressure
    assert pressure["count"] == 3, pressure
    data = json.loads(cap.state_path().read_text())
    assert "adaptive-sixth" not in data["leases"], "adaptive wait created a lease"

    pressure_payload = cap.current_rate_pressure()
    opencode_cap, opencode_pressure = cap.adaptive_agent_cap("opencode", 10, pressure_payload)
    assert opencode_cap == 10, opencode_pressure
    assert opencode_pressure is None, opencode_pressure

    data["leases"].update({
        _future_active_lease(idx, agent="grok-code")["lease_id"]: _future_active_lease(idx, agent="grok-code")
        for idx in range(5, 14)
    })
    cap.save_state(data)
    grok_buf = io.StringIO()
    with redirect_stdout(grok_buf):
        grok_rc = cap.main([
            "acquire",
            "--agent", "grok-code",
            "--lease-id", "adaptive-grok-tenth",
            "--project-root", str(REPO_ROOT),
            "--ttl-s", "3600",
            "--ram-mb", "65536",
            "--max-total", "20",
        ])
    assert grok_rc == 0, (
        "clustered codex model-capacity pressure must not reduce grok-code's static cap "
        f"(rc={grok_rc}, stdout={grok_buf.getvalue()})"
    )
    grok_payload = json.loads(grok_buf.getvalue())
    assert grok_payload["decision"] == "allow", grok_payload
    data = json.loads(cap.state_path().read_text())
    assert "adaptive-grok-tenth" in data["leases"], "grok-code full-cap acquire was not leased"


def case_adaptive_rate_pressure_status_surfaces_warning(state_dir: Path) -> None:
    """Capacity status surfaces the transient adaptive backoff warning."""
    _seed_codex_at_capacity_records(state_dir, count=3)
    cap.save_state(
        {
            "schema": cap.SCHEMA,
            "machine_id": cap.machine_id(),
            "leases": {},
            "cooldowns": {},
        }
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cap.main(["status", "--ram-mb", "65536", "--max-total", "20"])
    out = buf.getvalue()
    assert rc == 0, rc
    assert "warning: adaptive rate pressure agent:codex" in out, out
    base_cap = cap.DEFAULT_AGENT_CAPS["codex"]
    assert f"codex {base_cap}->{max(1, base_cap // 2)}" in out, out


def case_rate_pressure_refuses_per_session_policy_override(state_dir: Path) -> None:
    _seed_codex_at_capacity_records(state_dir, count=3)
    pressure = cap.current_rate_pressure(
        argparse.Namespace(rate_pressure_window_s=1, rate_pressure_threshold=99)
    )
    assert pressure["window_seconds"] == cap.DEFAULT_RATE_PRESSURE_WINDOW_SECONDS, pressure
    assert pressure["threshold"] == cap.DEFAULT_RATE_PRESSURE_THRESHOLD, pressure
    assert pressure["policy"]["override_mode"] == "refuse_per_session", pressure
    warnings = pressure.get("policy_warnings") or []
    assert any("window override" in item for item in warnings), warnings
    assert any("threshold override" in item for item in warnings), warnings


def case_ambiguous_pool_label_does_not_reduce_unrelated_seat_lane() -> None:
    """Measured path: multi-seat codex + account=default must not cap opencode*.

    Input path that reaches the asserted state (must match the live system):
      1. Multi-seat billing: three OpenAI accounts each declare codex* labels
         → agent_limit_pool_map sets pool_map['codex']=None (ambiguous).
      2. opencode* appears in no billing account → not in pool_map.
      3. pressure_per_provider on records agent=codex, account="default"
         (ACCOUNT_PLACEHOLDERS) → budget_key_for_record →
         budget_key_for_agent → provider-ambiguous:openai (NOT provider:openai).
      4. recommend() expands that key only to labels with pool_map[label]=None
         for openai (codex*), never opencode*.
      5. adaptive_agent_cap reads recommended_caps for those labels only.

    The prior green test hand-fed pool:openai-default, a key multi-seat
    billing never produces for codex/default.
    """
    shared_labels = ["codex", "codex-acp", "codex-bash-tail"]
    billing = {
        "accounts": [
            {
                "account_key": "25ca6b",
                "limit_pool_id": "openai-simon",
                "agent_labels": shared_labels,
            },
            {
                "account_key": "cf9f50",
                "limit_pool_id": "openai-tim",
                "agent_labels": shared_labels,
            },
            {
                "account_key": "d78343",
                "limit_pool_id": "openai-default",
                "agent_labels": shared_labels,
            },
        ],
    }
    pool_map = cap.goalflight_rate_pressure.agent_limit_pool_map(billing)
    for label in shared_labels:
        assert label in pool_map and pool_map[label] is None, pool_map
    for label in ("opencode", "opencode-acp", "opencode-bash-tail"):
        assert label not in pool_map, pool_map

    # Step 3: the key the system actually produces for codex + default.
    assert (
        cap.goalflight_rate_pressure.budget_key_for_record(
            {"account": "default"}, "codex", pool_map=pool_map
        )
        == "provider-ambiguous:openai"
    ), "codex/default must not fall through to the full provider roster key"

    now = time.time()
    recent_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 60))
    records = []
    for idx in range(3):
        records.append(
            {
                "dispatch_id": f"default-codex-{idx}",
                "agent": "codex",
                "state": "blocked_session_limit",
                "account": "default",
                "updated_at": recent_iso,
                "started_at": recent_iso,
                "status_path": None,
            }
        )
    counts = cap.goalflight_rate_pressure.pressure_per_provider(
        records, window_seconds=600, now_ts=now, pool_map=pool_map
    )
    assert counts == {"provider-ambiguous:openai": 3}, counts

    caps = {
        "codex": 20,
        "codex-acp": 15,
        "codex-bash-tail": 10,
        "opencode": 10,
        "opencode-acp": 10,
        "opencode-bash-tail": 10,
    }
    pressure = cap.goalflight_rate_pressure.recommend(
        counts, caps, threshold=3, pool_map=pool_map
    )
    entries = {entry["budget_key"]: entry for entry in pressure["providers_under_pressure"]}
    assert set(entries) == {"provider-ambiguous:openai"}, entries
    entry = entries["provider-ambiguous:openai"]
    assert entry["labels"] == shared_labels, entry
    assert entry["recommended_caps"] == {
        "codex": 10,
        "codex-acp": 7,
        "codex-bash-tail": 5,
    }, entry
    for open_label in ("opencode", "opencode-acp", "opencode-bash-tail"):
        assert open_label not in entry["labels"], entry
        assert open_label not in entry["recommended_caps"], entry

    # Capacity applies codex* reductions; opencode* stays at base.
    codex_cap, codex_detail = cap.adaptive_agent_cap("codex", 20, pressure)
    assert codex_cap == 10 and codex_detail is not None, (codex_cap, codex_detail)
    assert codex_detail["budget_key"] == "provider-ambiguous:openai", codex_detail
    for open_label in ("opencode", "opencode-acp", "opencode-bash-tail"):
        open_cap, open_detail = cap.adaptive_agent_cap(open_label, 10, pressure)
        assert open_cap == 10 and open_detail is None, (open_label, open_cap, open_detail)

    # Account-scoped pressure still emits no actuatable caps and does not
    # change label capacity (sibling-seat isolation).
    account_pressure = cap.goalflight_rate_pressure.recommend(
        {"account:openai:cf9f50": 3},
        caps,
        threshold=3,
        pool_map=pool_map,
    )
    account_entry = account_pressure["providers_under_pressure"][0]
    assert account_entry["scope"] == "account", account_entry
    assert account_entry["recommended_caps"] == {}, account_entry
    for label in shared_labels + ["opencode"]:
        base = caps.get(label, 10)
        label_cap, label_detail = cap.adaptive_agent_cap(label, base, account_pressure)
        assert label_cap == base and label_detail is None, (label, label_cap, label_detail)


def case_real_quota_tail_hard_stops_only_ledger_account(state_dir: Path) -> None:
    """Real billing/ledger vocabulary must never widen one seat into label caps.

    Input path:
      blocked_session_limit records with effective_account=cf9f50 + exhausted
      tail prose → pressure_per_provider → account:openai:cf9f50 → recommend
      (scope=account) → decorate_pressure_payload (stuck tails, hard_stop kinds).
    """
    isolated_state = state_dir / "real-quota-vocabulary"
    runs = isolated_state / "runs.d"
    tails = isolated_state / "tails"
    runs.mkdir(parents=True, exist_ok=True)
    tails.mkdir(parents=True, exist_ok=True)
    recent_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    def write_record(idx: int, effective_account: str, account: str) -> None:
        dispatch_id = f"quota-{effective_account}-{idx}"
        tail = tails / f"{dispatch_id}.tail"
        tail.write_text("ERROR: insufficient_quota; got 429\n", encoding="utf-8")
        (runs / f"{dispatch_id}.json").write_text(
            json.dumps(
                {
                    "schema": "goalflight.dispatch.v1",
                    "dispatch_id": dispatch_id,
                    "agent": "codex",
                    "state": "blocked_session_limit",
                    "account": account,
                    "effective_account": effective_account,
                    "started_at": recent_iso,
                    "updated_at": recent_iso,
                    "stdout_path": str(tail),
                    "status_path": None,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    for idx in range(3):
        write_record(idx, "cf9f50", "tim")
    # A recognized tail on another OpenAI seat must not be attached to the
    # thresholded cf9f50 entry merely because both records use label "codex".
    write_record(0, "25ca6b", "simon")

    billing = {
        "accounts": [
            {
                "account_key": "openai/simon",
                "limit_pool_id": "openai-simon",
                "agent_labels": ["codex", "codex-acp", "codex-bash-tail"],
            },
            {
                "account_key": "openai/tim",
                "limit_pool_id": "openai-tim",
                "agent_labels": ["codex", "codex-acp", "codex-bash-tail"],
            },
            {
                "account_key": "openai/default",
                "limit_pool_id": "openai-default",
                "agent_labels": ["codex", "codex-acp", "codex-bash-tail"],
            },
        ],
    }
    old_state = os.environ.get("GOALFLIGHT_STATE_DIR")
    os.environ["GOALFLIGHT_STATE_DIR"] = str(isolated_state)
    try:
        with mock.patch.object(cap.goalflight_rate_pressure, "load_billing_accounts", return_value=billing):
            pressure = cap.current_rate_pressure()
    finally:
        if old_state is None:
            os.environ.pop("GOALFLIGHT_STATE_DIR", None)
        else:
            os.environ["GOALFLIGHT_STATE_DIR"] = old_state

    entries = {entry["budget_key"]: entry for entry in pressure["providers_under_pressure"]}
    assert set(entries) == {"account:openai:cf9f50"}, entries
    entry = entries["account:openai:cf9f50"]
    assert entry["scope"] == "account", entry
    assert entry["provider"] == "openai", entry
    assert entry["label_resolution"]["status"] == "resolved_with_warning", entry
    assert entry["label_resolution"]["reason"] == "ledger_account_not_declared_in_billing", entry
    assert "openai/tim" in entry["label_resolution"]["declared_account_keys"], entry
    # Account scope must NOT look like an active capacity hold.
    assert entry["quota_hard_stop"] is False, entry
    # No cap-shaped field without an actuator (capacity skips account scope).
    assert "effective_account_cap" not in entry, entry
    assert "effective_caps" not in entry, entry
    assert entry.get("recommended_caps") == {}, entry
    advisory = entry.get("account_quota_advisory")
    assert isinstance(advisory, dict), entry
    assert advisory.get("enforced_by_capacity") is False, advisory
    assert advisory.get("account_key") == "cf9f50", advisory
    assert advisory.get("provider") == "openai", advisory
    assert "no automated consumer" in str(advisory.get("message") or ""), advisory
    assert "not enforced by the capacity gate" in str(advisory.get("message") or ""), advisory
    assert entry["stuck_worker_count"] == 3, entry
    assert {item["effective_account"] for item in entry["stuck_workers"]} == {"cf9f50"}, entry

    # Advisory text must not claim a hold capacity does not perform.
    advisory_text = cap.goalflight_quota_stuck.advisory_payload(entry)["text"]
    assert "holding new provider dispatch" not in advisory_text, advisory_text
    assert "advisory only" in advisory_text, advisory_text
    assert "capacity does not hold this lane" in advisory_text, advisory_text
    lines = cap.goalflight_quota_stuck.advisory_lines(pressure)
    assert lines, lines
    assert all("holding new provider dispatch" not in line for line in lines), lines
    assert any("advisory only" in line for line in lines), lines

    warnings = cap.rate_pressure_warnings(pressure)
    assert any("no automated consumer" in w for w in warnings), warnings
    assert not any("holding new provider dispatch" in w for w in warnings), warnings

    # One seat exhausted must leave every label cap untouched (healthy siblings
    # sharing those labels keep full capacity).
    for label in (
        "codex",
        "codex-acp",
        "codex-bash-tail",
        "opencode",
        "opencode-acp",
        "opencode-bash-tail",
    ):
        base = cap.DEFAULT_AGENT_CAPS.get(label, 5)
        label_cap, label_detail = cap.adaptive_agent_cap(label, base, pressure)
        assert label_cap == base, (label, label_cap, label_detail)
        assert label_detail is None, (label, label_detail)


def case_cross_label_exhausted_tail_does_not_hard_stop_ambiguous_lane(
    state_dir: Path,
) -> None:
    """P0: opencode exhausted must not hard-stop codex on provider-ambiguous.

    Input path (the measured inverted-cap failure):
      1. Multi-seat billing declares only codex* → pool_map[codex]=None
         → codex + account=default → provider-ambiguous:openai.
      2. Three codex blocked_session_limit records (no exhausted tails) hit
         threshold → recommend soft-reduces codex* only.
      3. One opencode record with an exhausted quota tail (NOT in the
         ambiguous entry's labels) is examined by decorate_pressure_payload.
      4. _entry_matches_record must NOT attach opencode via bare provider
         equality. Without that attachment there is no hard stop.
      5. adaptive_agent_cap("codex") stays at the soft half of base, not 0.
    """
    isolated = state_dir / "cross-label-ambiguous"
    runs = isolated / "runs.d"
    tails = isolated / "tails"
    runs.mkdir(parents=True, exist_ok=True)
    tails.mkdir(parents=True, exist_ok=True)
    recent_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    shared_labels = ["codex", "codex-acp", "codex-bash-tail"]
    billing = {
        "accounts": [
            {
                "account_key": "25ca6b",
                "limit_pool_id": "openai-simon",
                "agent_labels": shared_labels,
            },
            {
                "account_key": "cf9f50",
                "limit_pool_id": "openai-tim",
                "agent_labels": shared_labels,
            },
            {
                "account_key": "d78343",
                "limit_pool_id": "openai-default",
                "agent_labels": shared_labels,
            },
        ],
    }

    for idx in range(3):
        dispatch_id = f"ambig-codex-{idx}"
        (runs / f"{dispatch_id}.json").write_text(
            json.dumps(
                {
                    "schema": "goalflight.dispatch.v1",
                    "dispatch_id": dispatch_id,
                    "agent": "codex",
                    "state": "blocked_session_limit",
                    "account": "default",
                    "started_at": recent_iso,
                    "updated_at": recent_iso,
                    "status_path": None,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    # The sick lane: opencode exhausted, outside the ambiguous label set.
    open_id = "ambig-opencode-exhausted"
    open_tail = tails / f"{open_id}.tail"
    open_tail.write_text("ERROR: insufficient_quota; got 429\n", encoding="utf-8")
    (runs / f"{open_id}.json").write_text(
        json.dumps(
            {
                "schema": "goalflight.dispatch.v1",
                "dispatch_id": open_id,
                "agent": "opencode",
                "state": "rate_limited",
                "account": "default",
                "started_at": recent_iso,
                "updated_at": recent_iso,
                "stdout_path": str(open_tail),
                "status_path": None,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    old_state = os.environ.get("GOALFLIGHT_STATE_DIR")
    os.environ["GOALFLIGHT_STATE_DIR"] = str(isolated)
    try:
        with mock.patch.object(
            cap.goalflight_rate_pressure, "load_billing_accounts", return_value=billing
        ):
            pressure = cap.current_rate_pressure()
    finally:
        if old_state is None:
            os.environ.pop("GOALFLIGHT_STATE_DIR", None)
        else:
            os.environ["GOALFLIGHT_STATE_DIR"] = old_state

    entries = {e["budget_key"]: e for e in pressure["providers_under_pressure"]}
    assert "provider-ambiguous:openai" in entries, entries
    entry = entries["provider-ambiguous:openai"]
    assert entry["labels"] == shared_labels, entry
    # opencode must not attach; no exhausted match → no hard stop.
    stuck_agents = {item.get("agent") for item in entry.get("stuck_workers") or []}
    assert "opencode" not in stuck_agents, entry
    assert entry.get("quota_hard_stop") is False, entry
    assert "effective_caps" not in entry, entry

    base_codex = cap.DEFAULT_AGENT_CAPS["codex"]
    expected_soft = max(1, base_codex // 2)
    codex_cap, codex_detail = cap.adaptive_agent_cap("codex", base_codex, pressure)
    assert codex_cap == expected_soft, (codex_cap, codex_detail, entry)
    assert codex_detail is not None, codex_detail
    assert codex_detail["budget_key"] == "provider-ambiguous:openai", codex_detail
    assert codex_detail.get("quota_hard_stop") is False, codex_detail

    # opencode itself: count 1 under provider:openai is below threshold, so
    # no pressure entry and full base cap (the inverted bug left this uncapped
    # while zeroing codex — assert the healthy inverse).
    open_cap, open_detail = cap.adaptive_agent_cap(
        "opencode", cap.DEFAULT_AGENT_CAPS["opencode"], pressure
    )
    assert open_cap == cap.DEFAULT_AGENT_CAPS["opencode"], (open_cap, open_detail)
    assert open_detail is None, open_detail


def case_cross_pool_exhausted_does_not_hard_stop_other_pool(
    state_dir: Path,
) -> None:
    """A pool:openai-tim stuck worker must not hard-stop pool:openai-simon.

    Input path:
      1. Distinct single-pool label maps (no ambiguity): codex → openai-simon,
         codex-acp → openai-tim.
      2. Three codex blocked_session_limit records → pool:openai-simon at
         threshold (soft reduce only; no exhausted tails on simon).
      3. One codex-acp exhausted tail on pool openai-tim.
      4. decorate_pressure_payload must not attach the tim worker to the simon
         entry via bare provider equality.
      5. adaptive_agent_cap("codex") soft-halves; does not go to 0.
    """
    isolated = state_dir / "cross-pool-hard-stop"
    runs = isolated / "runs.d"
    tails = isolated / "tails"
    runs.mkdir(parents=True, exist_ok=True)
    tails.mkdir(parents=True, exist_ok=True)
    recent_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    billing = {
        "accounts": [
            {
                "account_key": "openai/simon",
                "limit_pool_id": "openai-simon",
                "agent_labels": ["codex"],
            },
            {
                "account_key": "openai/tim",
                "limit_pool_id": "openai-tim",
                "agent_labels": ["codex-acp"],
            },
        ],
    }
    pool_map = cap.goalflight_rate_pressure.agent_limit_pool_map(billing)
    assert pool_map.get("codex") == "openai-simon", pool_map
    assert pool_map.get("codex-acp") == "openai-tim", pool_map
    assert (
        cap.goalflight_rate_pressure.budget_key_for_agent("codex", pool_map=pool_map)
        == "pool:openai-simon"
    )
    assert (
        cap.goalflight_rate_pressure.budget_key_for_agent("codex-acp", pool_map=pool_map)
        == "pool:openai-tim"
    )

    for idx in range(3):
        dispatch_id = f"pool-simon-codex-{idx}"
        (runs / f"{dispatch_id}.json").write_text(
            json.dumps(
                {
                    "schema": "goalflight.dispatch.v1",
                    "dispatch_id": dispatch_id,
                    "agent": "codex",
                    "state": "blocked_session_limit",
                    "account": "default",
                    "started_at": recent_iso,
                    "updated_at": recent_iso,
                    "status_path": None,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    tim_id = "pool-tim-acp-exhausted"
    tim_tail = tails / f"{tim_id}.tail"
    tim_tail.write_text("ERROR: insufficient_quota; got 429\n", encoding="utf-8")
    (runs / f"{tim_id}.json").write_text(
        json.dumps(
            {
                "schema": "goalflight.dispatch.v1",
                "dispatch_id": tim_id,
                "agent": "codex-acp",
                "state": "rate_limited",
                "account": "default",
                "started_at": recent_iso,
                "updated_at": recent_iso,
                "stdout_path": str(tim_tail),
                "status_path": None,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    old_state = os.environ.get("GOALFLIGHT_STATE_DIR")
    os.environ["GOALFLIGHT_STATE_DIR"] = str(isolated)
    try:
        with mock.patch.object(
            cap.goalflight_rate_pressure, "load_billing_accounts", return_value=billing
        ):
            pressure = cap.current_rate_pressure()
    finally:
        if old_state is None:
            os.environ.pop("GOALFLIGHT_STATE_DIR", None)
        else:
            os.environ["GOALFLIGHT_STATE_DIR"] = old_state

    entries = {e["budget_key"]: e for e in pressure["providers_under_pressure"]}
    assert "pool:openai-simon" in entries, entries
    simon = entries["pool:openai-simon"]
    assert simon["labels"] == ["codex"], simon
    stuck_agents = {item.get("agent") for item in simon.get("stuck_workers") or []}
    assert "codex-acp" not in stuck_agents, simon
    assert simon.get("quota_hard_stop") is False, simon

    base_codex = cap.DEFAULT_AGENT_CAPS["codex"]
    expected_soft = max(1, base_codex // 2)
    codex_cap, codex_detail = cap.adaptive_agent_cap("codex", base_codex, pressure)
    assert codex_cap == expected_soft, (codex_cap, codex_detail, simon)
    assert codex_detail is not None and codex_detail.get("quota_hard_stop") is False, codex_detail


def case_matching_exhausted_tail_still_hard_stops_own_labels(
    state_dir: Path,
) -> None:
    """Genuinely matching stuck workers still hard-stop their own labels.

    Input path:
      1. Multi-seat billing → codex* → provider-ambiguous:openai.
      2. Three codex records with exhausted tails (state blocked_session_limit).
      3. decorate_pressure_payload attaches those codex workers (agent in
         labels / budget key equals entry) → quota_hard_stop + effective_caps 0.
      4. adaptive_agent_cap("codex") → 0. opencode stays at base (not in labels).
    """
    isolated = state_dir / "matching-hard-stop"
    runs = isolated / "runs.d"
    tails = isolated / "tails"
    runs.mkdir(parents=True, exist_ok=True)
    tails.mkdir(parents=True, exist_ok=True)
    recent_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    shared_labels = ["codex", "codex-acp", "codex-bash-tail"]
    billing = {
        "accounts": [
            {
                "account_key": "25ca6b",
                "limit_pool_id": "openai-simon",
                "agent_labels": shared_labels,
            },
            {
                "account_key": "cf9f50",
                "limit_pool_id": "openai-tim",
                "agent_labels": shared_labels,
            },
            {
                "account_key": "d78343",
                "limit_pool_id": "openai-default",
                "agent_labels": shared_labels,
            },
        ],
    }

    for idx in range(3):
        dispatch_id = f"match-codex-{idx}"
        tail = tails / f"{dispatch_id}.tail"
        tail.write_text("ERROR: insufficient_quota; got 429\n", encoding="utf-8")
        (runs / f"{dispatch_id}.json").write_text(
            json.dumps(
                {
                    "schema": "goalflight.dispatch.v1",
                    "dispatch_id": dispatch_id,
                    "agent": "codex",
                    "state": "blocked_session_limit",
                    "account": "default",
                    "started_at": recent_iso,
                    "updated_at": recent_iso,
                    "stdout_path": str(tail),
                    "status_path": None,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    old_state = os.environ.get("GOALFLIGHT_STATE_DIR")
    os.environ["GOALFLIGHT_STATE_DIR"] = str(isolated)
    try:
        with mock.patch.object(
            cap.goalflight_rate_pressure, "load_billing_accounts", return_value=billing
        ):
            pressure = cap.current_rate_pressure()
    finally:
        if old_state is None:
            os.environ.pop("GOALFLIGHT_STATE_DIR", None)
        else:
            os.environ["GOALFLIGHT_STATE_DIR"] = old_state

    entries = {e["budget_key"]: e for e in pressure["providers_under_pressure"]}
    assert set(entries) == {"provider-ambiguous:openai"}, entries
    entry = entries["provider-ambiguous:openai"]
    assert entry["labels"] == shared_labels, entry
    assert entry.get("quota_hard_stop") is True, entry
    assert entry.get("stuck_worker_count") == 3, entry
    assert entry.get("effective_caps") == {label: 0 for label in shared_labels}, entry
    stuck_agents = {item.get("agent") for item in entry.get("stuck_workers") or []}
    assert stuck_agents == {"codex"}, stuck_agents

    base_codex = cap.DEFAULT_AGENT_CAPS["codex"]
    codex_cap, codex_detail = cap.adaptive_agent_cap("codex", base_codex, pressure)
    assert codex_cap == 0, (codex_cap, codex_detail)
    assert codex_detail is not None and codex_detail.get("quota_hard_stop") is True, codex_detail

    for open_label in ("opencode", "opencode-acp", "opencode-bash-tail"):
        base = cap.DEFAULT_AGENT_CAPS.get(open_label, 10)
        open_cap, open_detail = cap.adaptive_agent_cap(open_label, base, pressure)
        assert open_cap == base, (open_label, open_cap, open_detail)
        assert open_detail is None, (open_label, open_detail)


def case_label_set_match_hard_stops_when_budget_key_path_unavailable() -> None:
    """Label-set matching is load-bearing when pool_map is absent at decorate.

    Input path:
      1. recommend() with multi-seat pool_map builds provider-ambiguous:openai
         with labels=[codex*] and soft caps.
      2. decorate_pressure_payload is called with pool_map=None (budget_key path
         for codex becomes provider:openai, not provider-ambiguous:openai).
      3. Attachment therefore depends on agent-in-labels.
      4. adaptive_agent_cap("codex") → 0 hard stop; opencode stays at base.
    """
    shared_labels = ["codex", "codex-acp", "codex-bash-tail"]
    billing = {
        "accounts": [
            {
                "account_key": "25ca6b",
                "limit_pool_id": "openai-simon",
                "agent_labels": shared_labels,
            },
            {
                "account_key": "cf9f50",
                "limit_pool_id": "openai-tim",
                "agent_labels": shared_labels,
            },
        ],
    }
    pool_map = cap.goalflight_rate_pressure.agent_limit_pool_map(billing)
    assert pool_map.get("codex") is None, pool_map

    now = time.time()
    recent_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 30))
    records: list[dict] = []
    with tempfile.TemporaryDirectory() as td:
        tails = Path(td) / "tails"
        tails.mkdir()
        for idx in range(3):
            dispatch_id = f"label-only-{idx}"
            tail = tails / f"{dispatch_id}.tail"
            tail.write_text("ERROR: insufficient_quota; got 429\n", encoding="utf-8")
            records.append(
                {
                    "schema": "goalflight.dispatch.v1",
                    "dispatch_id": dispatch_id,
                    "agent": "codex",
                    "state": "blocked_session_limit",
                    "account": "default",
                    "started_at": recent_iso,
                    "updated_at": recent_iso,
                    "stdout_path": str(tail),
                    "status_path": None,
                }
            )
        # Cross-label noise: must still not attach without label membership.
        open_tail = tails / "label-only-open.tail"
        open_tail.write_text("ERROR: insufficient_quota; got 429\n", encoding="utf-8")
        records.append(
            {
                "schema": "goalflight.dispatch.v1",
                "dispatch_id": "label-only-open",
                "agent": "opencode",
                "state": "rate_limited",
                "account": "default",
                "started_at": recent_iso,
                "updated_at": recent_iso,
                "stdout_path": str(open_tail),
                "status_path": None,
            }
        )

        counts = cap.goalflight_rate_pressure.pressure_per_provider(
            records, window_seconds=600, now_ts=now, pool_map=pool_map
        )
        assert counts.get("provider-ambiguous:openai") == 3, counts
        payload = cap.goalflight_rate_pressure.recommend(
            counts,
            dict(cap.DEFAULT_AGENT_CAPS),
            threshold=3,
            pool_map=pool_map,
        )
        # Decorate without pool_map so budget_key equality cannot rescue codex.
        pressure = cap.goalflight_quota_stuck.decorate_pressure_payload(
            payload,
            records,
            window_seconds=600,
            pool_map=None,
        )

    entries = {e["budget_key"]: e for e in pressure["providers_under_pressure"]}
    entry = entries["provider-ambiguous:openai"]
    stuck_agents = {item.get("agent") for item in entry.get("stuck_workers") or []}
    assert stuck_agents == {"codex"}, (stuck_agents, entry)
    assert "opencode" not in stuck_agents, entry
    assert entry.get("quota_hard_stop") is True, entry
    assert entry.get("effective_caps") == {label: 0 for label in shared_labels}, entry

    base_codex = cap.DEFAULT_AGENT_CAPS["codex"]
    codex_cap, codex_detail = cap.adaptive_agent_cap("codex", base_codex, pressure)
    assert codex_cap == 0, (codex_cap, codex_detail)
    assert codex_detail is not None and codex_detail.get("quota_hard_stop") is True, codex_detail

    open_cap, open_detail = cap.adaptive_agent_cap(
        "opencode", cap.DEFAULT_AGENT_CAPS["opencode"], pressure
    )
    assert open_cap == cap.DEFAULT_AGENT_CAPS["opencode"], (open_cap, open_detail)
    assert open_detail is None, open_detail


def case_empty_state_dir_falls_back_not_cwd() -> None:
    """A present-but-empty (or whitespace-only) GOALFLIGHT_STATE_DIR must resolve
    to DEFAULT_STATE_DIR, NOT cwd. Regression: os.environ.get(key, default)
    returns "" for a present-but-empty key, and Path("").expanduser() == Path(".")
    (cwd), which scatters capacity.json / capacity.lock into the working dir.
    """
    old_env = os.environ.get("GOALFLIGHT_STATE_DIR")
    old_default = cap.DEFAULT_STATE_DIR
    old_default_fn = compat.default_state_dir
    old_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as td:
        default_dir = Path(td) / "default-state"
        work_dir = Path(td) / "work"
        work_dir.mkdir()
        cap.DEFAULT_STATE_DIR = default_dir
        compat.default_state_dir = lambda: default_dir
        os.chdir(work_dir)
        try:
            for blank in ("", "   "):
                os.environ["GOALFLIGHT_STATE_DIR"] = blank
                resolved = cap.state_dir()
                assert resolved == default_dir, (
                    f"blank env {blank!r} -> {resolved}, expected DEFAULT {default_dir}"
                )
                assert resolved.resolve() != work_dir.resolve(), (
                    "blank env must NOT resolve to cwd"
                )
            # an explicit value is still honored
            explicit = Path(td) / "explicit"
            os.environ["GOALFLIGHT_STATE_DIR"] = str(explicit)
            assert cap.state_dir() == explicit
        finally:
            os.chdir(old_cwd)
            cap.DEFAULT_STATE_DIR = old_default
            compat.default_state_dir = old_default_fn
            if old_env is None:
                os.environ.pop("GOALFLIGHT_STATE_DIR", None)
            else:
                os.environ["GOALFLIGHT_STATE_DIR"] = old_env


def main() -> None:
    # Pure in-memory prune cases (no shared-state IO at all).
    case_live_worker_past_ttl_survives_prune()
    case_live_worker_dead_controller_survives_prune()
    case_dead_lease_past_ttl_is_reclaimed()
    case_dead_lease_no_ttl_not_expired()
    case_rate_limited_retained_lease_pruned()
    case_indeterminate_holder_bounded_not_indefinite()
    case_live_worker_survives_past_indeterminate_retention()
    case_unprobeable_retained_scope_reclaims_after_until()
    case_dead_worker_reclaimed_inside_indeterminate_retention()
    case_stale_active_leases_live_worker_not_stale_with_dead_controller()
    case_empty_state_dir_falls_back_not_cwd()
    case_capacity_wait_resolution_precedence()
    case_acquire_with_wait_zero_preserves_single_shot_payload()
    case_acquire_with_wait_jitter_bounds_and_deadline_math()
    case_acquire_with_wait_signal_interrupts_sleep_promptly()
    case_ambiguous_pool_label_does_not_reduce_unrelated_seat_lane()
    case_label_set_match_hard_stops_when_budget_key_path_unavailable()

    # IO cases: isolate capacity.json under a temp $GOALFLIGHT_STATE_DIR so the
    # real shared /tmp/goal-flight-<uid>/capacity.json is never touched.
    old = os.environ.get("GOALFLIGHT_STATE_DIR")
    old_threshold = os.environ.get("GOALFLIGHT_RATE_PRESSURE_THRESHOLD")
    old_window = os.environ.get("GOALFLIGHT_RATE_PRESSURE_WINDOW_SECONDS")
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        os.environ["GOALFLIGHT_STATE_DIR"] = str(state_dir)
        os.environ["GOALFLIGHT_RATE_PRESSURE_THRESHOLD"] = "3"
        os.environ["GOALFLIGHT_RATE_PRESSURE_WINDOW_SECONDS"] = "600"
        try:
            case_real_quota_tail_hard_stops_only_ledger_account(state_dir)
            case_cross_label_exhausted_tail_does_not_hard_stop_ambiguous_lane(state_dir)
            case_cross_pool_exhausted_does_not_hard_stop_other_pool(state_dir)
            case_matching_exhausted_tail_still_hard_stops_own_labels(state_dir)
            case_status_is_non_mutating_for_live_lease(state_dir)
            case_status_still_reclaims_dead_lease_in_view(state_dir)
            case_release_stale_poison_pair_live_worker_survives_dead_controller(state_dir)
            case_aggregate_status_payload_does_not_persist_prune(state_dir)
            case_acquire_atomic_gate_still_blocks_over_cap(state_dir)
            case_unowned_acquire_tracks_pre_attach_claimant(state_dir)
            case_adaptive_rate_pressure_reduces_codex_effective_cap(state_dir)
            case_adaptive_rate_pressure_status_surfaces_warning(state_dir)
            case_rate_pressure_refuses_per_session_policy_override(state_dir)
        finally:
            if old is None:
                os.environ.pop("GOALFLIGHT_STATE_DIR", None)
            else:
                os.environ["GOALFLIGHT_STATE_DIR"] = old
            if old_threshold is None:
                os.environ.pop("GOALFLIGHT_RATE_PRESSURE_THRESHOLD", None)
            else:
                os.environ["GOALFLIGHT_RATE_PRESSURE_THRESHOLD"] = old_threshold
            if old_window is None:
                os.environ.pop("GOALFLIGHT_RATE_PRESSURE_WINDOW_SECONDS", None)
            else:
                os.environ["GOALFLIGHT_RATE_PRESSURE_WINDOW_SECONDS"] = old_window

    print("OK: capacity TTL liveness-gate + non-mutating status tests pass")


if __name__ == "__main__":
    main()
