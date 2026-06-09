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
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import goalflight_capacity as cap  # noqa: E402
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


def case_adaptive_rate_pressure_reduces_codex_effective_cap(state_dir: Path) -> None:
    """Clustered codex model-capacity failures halve only codex effective cap."""
    _seed_codex_at_capacity_records(state_dir, count=3)
    leases = {_future_active_lease(idx)["lease_id"]: _future_active_lease(idx) for idx in range(5)}
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
    assert rc == 2, f"adaptive sixth codex acquire should wait (rc={rc}, stdout={buf.getvalue()})"
    payload = json.loads(buf.getvalue())
    assert payload["reason"] == "adaptive_rate_pressure", payload
    assert payload["active"] == 5, payload
    assert payload["base_agent_cap"] == 10, payload
    assert payload["agent_cap"] == 5, payload
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
    assert "codex 10->5" in out, out


def case_empty_state_dir_falls_back_not_cwd() -> None:
    """A present-but-empty (or whitespace-only) GOALFLIGHT_STATE_DIR must resolve
    to DEFAULT_STATE_DIR, NOT cwd. Regression: os.environ.get(key, default)
    returns "" for a present-but-empty key, and Path("").expanduser() == Path(".")
    (cwd), which scatters capacity.json / capacity.lock into the working dir.
    """
    old_env = os.environ.get("GOALFLIGHT_STATE_DIR")
    old_default = cap.DEFAULT_STATE_DIR
    old_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as td:
        default_dir = Path(td) / "default-state"
        work_dir = Path(td) / "work"
        work_dir.mkdir()
        cap.DEFAULT_STATE_DIR = default_dir
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
    case_empty_state_dir_falls_back_not_cwd()

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
            case_status_is_non_mutating_for_live_lease(state_dir)
            case_status_still_reclaims_dead_lease_in_view(state_dir)
            case_aggregate_status_payload_does_not_persist_prune(state_dir)
            case_acquire_atomic_gate_still_blocks_over_cap(state_dir)
            case_adaptive_rate_pressure_reduces_codex_effective_cap(state_dir)
            case_adaptive_rate_pressure_status_surfaces_warning(state_dir)
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
