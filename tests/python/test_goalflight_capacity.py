"""Focused tests for capacity TTL-prune liveness gate + non-mutating status.

Covers two fixes (B-P0, B-P1) in scripts/goalflight_capacity.py:

  B-P0  prune_state must NOT TTL-expire a lease whose worker or orchestrator pid
        is still LIVE. capacity.json is shared across sibling projects under one
        /tmp/goal-flight-<uid>/ dir; a clock-only TTL eviction here would
        over-subscribe the machine while a sibling project's worker is still
        consuming RAM. Only a lease with BOTH pids dead is reclaimable by TTL.

  B-P1  cmd_status is a READ. It must compute a pruned VIEW without persisting,
        so a frequent status poll can't race-evict another project's live lease.

State is isolated via $GOALFLIGHT_STATE_DIR (read at call time by state_dir()),
so these tests never read or mutate the real shared capacity.json.
"""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows(
    "capacity liveness tests spawn a real subprocess and probe pid liveness"
)

import datetime as dt
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import goalflight_capacity as cap  # noqa: E402


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


def main() -> None:
    # Pure in-memory prune cases (no shared-state IO at all).
    case_live_worker_past_ttl_survives_prune()
    case_live_worker_dead_controller_survives_prune()
    case_dead_lease_past_ttl_is_reclaimed()
    case_dead_lease_no_ttl_not_expired()

    # IO cases: isolate capacity.json under a temp $GOALFLIGHT_STATE_DIR so the
    # real shared /tmp/goal-flight-<uid>/capacity.json is never touched.
    old = os.environ.get("GOALFLIGHT_STATE_DIR")
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        os.environ["GOALFLIGHT_STATE_DIR"] = str(state_dir)
        try:
            case_status_is_non_mutating_for_live_lease(state_dir)
            case_status_still_reclaims_dead_lease_in_view(state_dir)
            case_acquire_atomic_gate_still_blocks_over_cap(state_dir)
        finally:
            if old is None:
                os.environ.pop("GOALFLIGHT_STATE_DIR", None)
            else:
                os.environ["GOALFLIGHT_STATE_DIR"] = old

    print("OK: capacity TTL liveness-gate + non-mutating status tests pass")


if __name__ == "__main__":
    main()
