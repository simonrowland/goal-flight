#!/usr/bin/env python3
"""Machine-global capacity coordinator for goal-flight dispatches."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import platform
import random
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid

import goalflight_compat
import goalflight_compat as fcntl
import goalflight_rate_pressure
from goalflight_liveness import active_monotonic

SCHEMA = "goalflight.capacity.v1"


def _default_state_dir() -> Path:
    return goalflight_compat.default_state_dir()


DEFAULT_STATE_DIR = Path(os.environ.get("GOALFLIGHT_STATE_DIR", _default_state_dir()))
DEFAULT_RESERVE_MB = 2048
DEFAULT_WORST_WORKER_MB = 1200
# DEFAULT_HARD_CAP raised 20->40 (2026-06-16, operator-requested): (1) unmask the
# per-agent codex/grok bumps below, and (2) feed the deep-research build's headroom.
# This is the raw_ceiling INPUT; operating_cap = min(raw_ceiling, tier), and the >64GB
# tier is raised to 32 (operating_cap_for_ram), so the effective machine total lands at
# 32. raw_ceiling still = min(this, headroom_mb // worst_worker_mb), so RAM (128GB here)
# and the acquire-time RSS budget continue to bound real concurrency.
DEFAULT_HARD_CAP = 40
DEFAULT_RATE_PRESSURE_WINDOW_SECONDS = 600
DEFAULT_RATE_PRESSURE_THRESHOLD = 3
AGENT_RSS_MB = {
    "grok": 111,
    "grok-acp": 111,
    "grok-code": 111,
    "grok-research": 111,
    "codex": 386,
    "codex-acp": 386,
    "claude": 614,
    "claude-code-cli-acp": 614,
    "cursor": 1203,
    "cursor-agent": 1203,
    "opencode": 386,
    "opencode-acp": 386,
    "opencode-bash-tail": 386,
}
# Per-agent concurrency caps, machine-global across goal-flight sessions.
# Sized to support multi-session parallel work. The adaptive busy-signal
# walkback (scripts/goalflight_rate_pressure.py) reduces effective caps at
# acquire-time when recent dispatch-ledger failures show provider pressure.
# Static caps below remain starting defaults; the adaptive cap is transient and
# never mutates this map or capacity.json.
DEFAULT_AGENT_CAPS = {
    # cursor-agent talks to Cursor's CLOUD backend, which is SLOW: a trivial prompt
    # takes ~34s solo and ~57s at 3-concurrent, the whole time at ~0% CPU (blocked
    # on the network). It DOES run concurrently (3 parallel complete together) but
    # reliably only up to ~3 — at 5 the mid-stream gaps between chunks exceed the
    # heartbeat wedge window. The first-token grace (heartbeat_wedge_decision
    # requires wedge_progress_seen>=1) stops false-kills before the first token;
    # cap 3 covers the steady-state slowness. (Stress-tested 2026-05-20: 1/2/3
    # clean, 5 -> 2 recoverable wedges, zero orphan leaks.) cursor and cursor-agent
    # share one Cursor subscription budget.
    "cursor": 3,
    "cursor-agent": 3,
    "opencode": 10,
    "opencode-acp": 10,
    "opencode-bash-tail": 10,
    # claude-code-cli-acp PTY-drives the interactive Claude TUI and tails the
    # session transcript with a HARDCODED 120s per-turn timeout (not exposed in
    # ACP-server mode). 2026-05-20: 4 SIMULTANEOUS dispatches starve each other on
    # TUI startup (hooks/LSP/keychain/auto-memory/MCP) — even a trivial turn
    # exceeded 120s (3/4); the SAME 4 with serialized startups ran 4/4. The
    # contention is STARTUP, not steady-state. goalflight_startup_gate.StartupGate
    # now serializes the spawn→handshake window for this adapter (handshake-gated,
    # machine-agnostic — no hardcoded stagger interval), so concurrent TURNS are
    # safe and the count cap can stay at 5. (`--bare` can't help — it forces
    # ANTHROPIC_API_KEY, breaking subscription/OAuth auth. For very high Claude
    # parallelism, the Agent tool is still the native path — no adapter/PTY.)
    "claude": 5,
    "claude-code-cli-acp": 5,
    # codex loosened 10->12 (2026-06-10): monitored ~1h window through three
    # multi-controller review storms — 483 dispatch-ledger records, zero
    # providers under pressure, no saturation alerts. Stress-tested earlier at
    # 49/49 + 13/13 TRUE-simultaneous with zero wedges (2026-05-20); the
    # adaptive walk-back still halves effective caps on real rejections.
    # codex loosened 12->18 (2026-06-16, operator-requested push): watched LIVE via
    # goalflight_rate_pressure.py (clean baseline: 826 ledger records, 0 providers
    # under pressure) + the adaptive walk-back backstop. Revisit after logged hours.
    "codex": 18,
    "codex-acp": 18,
    # grok loosened 10->14 (2026-06-10): multi-controller queueing observed with
    # ZERO rate-pressure evidence (449 ledger records, no providers under
    # pressure); grok is sub-billed, and the adaptive walk-back halves effective
    # caps at acquire-time if rejections appear. Revisit after more logged hours.
    # grok loosened 14->20 (2026-06-16, operator-requested push): same live monitor.
    "grok": 20,
    "grok-acp": 20,
    "grok-code": 20,
    "grok-research": 20,
    # Gateway orchestrators: lower cap, longer orchestration latency (Track D).
    "herm-worker": 2,
    "cla-worker": 2,
    "paperclip": 2,
}
# Priority lanes (2026-06-10): acquire is single-shot try-or-block (no queue),
# so under multi-controller bursts ("review storms") bulk retries statistically
# crowd out critical fix dispatches. Lanes reserve headroom instead of queueing:
#   bulk     — may not take the last BULK_GLOBAL_RESERVE machine slots nor the
#              last BULK_POOL_RESERVE slot of its agent pool. Review storms
#              SHOULD dispatch with --priority bulk.
#   normal   — default; exactly today's behavior.
#   critical — may borrow CRITICAL_*_BORROW slots beyond the operating/pool cap
#              (never beyond the RAM raw ceiling; pool borrow is DISABLED while
#              adaptive rate-pressure is active — provider pushback wins).
PRIORITY_LANES = ("critical", "normal", "bulk")
BULK_GLOBAL_RESERVE = 3
BULK_POOL_RESERVE = 1
CRITICAL_GLOBAL_BORROW = 2
CRITICAL_POOL_BORROW = 2

# Bounded capacity-wait defaults per priority lane (seconds). This is
# contention polling, not a FIFO queue.
CAPACITY_WAIT_DEFAULTS_S = {"bulk": 900, "normal": 600, "critical": 120}
CAPACITY_WAIT_POLL_S = 15.0
CAPACITY_WAIT_JITTER_S = 2.0
CAPACITY_WAIT_SLEEP_SLICE_S = 0.5


class CapacityWaitInterrupted(Exception):
    """Raised when SIGTERM/SIGINT interrupts acquire_with_wait."""

    def __init__(self, payload: dict, *, exit_code: int | None = None, signum: int | None = None) -> None:
        super().__init__(payload.get("reason", "wait_interrupted"))
        self.payload = payload
        self.exit_code = exit_code
        self.signum = signum


def _capacity_wait_interrupted(
    *,
    wait_started: float,
    attempt: int,
    monotonic_fn,
    exit_code: int | None = None,
    signum: int | None = None,
) -> CapacityWaitInterrupted:
    payload = {
        "decision": "wait",
        "reason": "wait_interrupted",
        "waited_s": round(monotonic_fn() - wait_started, 1),
        "attempts": attempt,
    }
    return CapacityWaitInterrupted(payload, exit_code=exit_code, signum=signum)


def resolve_capacity_wait_s(
    *,
    lane: str | None,
    wait_s: float | int | None,
    env: dict[str, str] | None = None,
    log_prefix: str | None = None,
    stderr=None,
) -> float:
    """Resolve capacity wait budget: explicit wait > env > lane default."""

    if wait_s is not None:
        return max(0.0, float(wait_s))
    env_map = os.environ if env is None else env
    env_override = env_map.get("GOALFLIGHT_CAPACITY_WAIT_S")
    if env_override not in (None, ""):
        try:
            value = max(0.0, float(env_override))
        except ValueError:
            if log_prefix:
                print(
                    f"{log_prefix}: ignoring invalid GOALFLIGHT_CAPACITY_WAIT_S={env_override!r}",
                    file=stderr or sys.stderr,
                )
        else:
            if log_prefix:
                print(
                    f"{log_prefix}: capacity wait {value}s from GOALFLIGHT_CAPACITY_WAIT_S",
                    file=stderr or sys.stderr,
                )
            return value
    lane_key = (lane or "normal").strip().lower()
    return float(CAPACITY_WAIT_DEFAULTS_S.get(lane_key, CAPACITY_WAIT_DEFAULTS_S["normal"]))


def _cmd_acquire_payload(
    acquire_args: argparse.Namespace,
    *,
    acquire_func=None,
) -> tuple[int, dict]:
    acquire_out = io.StringIO()
    with contextlib.redirect_stdout(acquire_out):
        rc = (acquire_func or cmd_acquire)(acquire_args)
    try:
        payload = json.loads(acquire_out.getvalue() or "{}")
    except json.JSONDecodeError:
        payload = {"raw": acquire_out.getvalue()}
    return rc, payload


def _sleep_bounded(total_s: float, *, sleep_fn, slice_s: float = CAPACITY_WAIT_SLEEP_SLICE_S) -> None:
    remaining_s = max(0.0, float(total_s))
    slice_budget_s = max(0.001, float(slice_s))
    while remaining_s > 0:
        chunk_s = min(remaining_s, slice_budget_s)
        sleep_fn(chunk_s)
        remaining_s = max(0.0, remaining_s - chunk_s)


def acquire_with_wait(
    acquire_args: argparse.Namespace,
    *,
    lane: str | None,
    wait_s: float,
    poll_s: float = CAPACITY_WAIT_POLL_S,
    jitter: float = CAPACITY_WAIT_JITTER_S,
    on_wait=None,
    install_signal_handlers: bool = False,
    monotonic_fn=active_monotonic,
    sleep_fn=time.sleep,
    random_fn=random.uniform,
    acquire_func=None,
) -> dict:
    """Acquire capacity, polling bounded wait decisions until budget expires.

    The helper deliberately performs no ledger/status writes. Callers can use
    on_wait(attempt, remaining_s, reason) for their own progress side effects.
    """

    wait_budget_s = max(0.0, float(wait_s))
    wait_started = monotonic_fn()
    deadline = wait_started + wait_budget_s if wait_budget_s > 0 else None
    attempt = 0

    def _wait_signal(signum, _frame):
        raise CapacityWaitInterrupted({}, exit_code=128 + signum, signum=signum)

    old_handlers = {}
    if install_signal_handlers:
        for signame in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, signame, None)
            if sig is not None:
                with contextlib.suppress(Exception):
                    old_handlers[sig] = signal.signal(sig, _wait_signal)
    try:
        while True:
            attempt += 1
            rc, payload = _cmd_acquire_payload(acquire_args, acquire_func=acquire_func)
            if rc == 0:
                return payload
            now = monotonic_fn()
            can_wait = (
                payload.get("decision") == "wait"
                and deadline is not None
                and now < deadline
            )
            if not can_wait:
                return payload
            remaining_s = max(0.0, deadline - now)
            if on_wait is not None:
                on_wait(attempt, remaining_s, payload)
            sleep_for = float(poll_s) + (random_fn(0.0, float(jitter)) if jitter > 0 else 0.0)
            if remaining_s > 0:
                sleep_for = min(sleep_for, remaining_s)
            _sleep_bounded(sleep_for, sleep_fn=sleep_fn)
    except (CapacityWaitInterrupted, KeyboardInterrupt) as exc:
        exit_code = getattr(exc, "exit_code", None)
        signum = getattr(exc, "signum", None)
        raise _capacity_wait_interrupted(
            wait_started=wait_started,
            attempt=attempt,
            monotonic_fn=monotonic_fn,
            exit_code=exit_code,
            signum=signum,
        ) from None
    finally:
        for sig, old in old_handlers.items():
            with contextlib.suppress(Exception):
                signal.signal(sig, old)


async def acquire_with_wait_async(
    acquire_args: argparse.Namespace,
    *,
    lane: str | None,
    wait_s: float,
    poll_s: float = CAPACITY_WAIT_POLL_S,
    jitter: float = CAPACITY_WAIT_JITTER_S,
    on_wait=None,
    interrupted=None,
    interrupted_signum=None,
    monotonic_fn=active_monotonic,
    sleep_fn=asyncio.sleep,
    random_fn=random.uniform,
    acquire_func=None,
) -> dict:
    """Async capacity wait for callers already inside an event loop."""

    wait_budget_s = max(0.0, float(wait_s))
    wait_started = monotonic_fn()
    deadline = wait_started + wait_budget_s if wait_budget_s > 0 else None
    attempt = 0
    try:
        while True:
            attempt += 1
            rc, payload = _cmd_acquire_payload(acquire_args, acquire_func=acquire_func)
            if rc == 0:
                return payload
            now = monotonic_fn()
            can_wait = (
                payload.get("decision") == "wait"
                and deadline is not None
                and now < deadline
            )
            if not can_wait:
                return payload
            remaining_s = max(0.0, deadline - now)
            if on_wait is not None:
                on_wait(attempt, remaining_s, payload)
            sleep_for = float(poll_s) + (random_fn(0.0, float(jitter)) if jitter > 0 else 0.0)
            if remaining_s > 0:
                sleep_for = min(sleep_for, remaining_s)
            await sleep_fn(max(0.0, sleep_for))
    except asyncio.CancelledError:
        if interrupted is None or not interrupted():
            raise
        signum = interrupted_signum() if interrupted_signum is not None else None
        raise _capacity_wait_interrupted(
            wait_started=wait_started,
            attempt=attempt,
            monotonic_fn=monotonic_fn,
            exit_code=128 + signum if signum is not None else None,
            signum=signum,
        ) from None

TERMINAL_LEASE_STATES = {
    "released",
    "expired",
    "complete",
    "failed",
    "wedged",
    "tool_timeout",
    # Legacy 0.4.3 terminal state. Current ACP oversized frames drop and
    # continue; keep this so old lease records still prune.
    "result_too_large",
    "blocked",
    "blocked_capacity",
    "blocked_session_limit",
    "blocked_auth",
    "worker_dead",
    "idle_timeout",
    "controller_dead",
    "orphaned",
    "inconclusive_timeout",
    "inconclusive_no_final",
    "superseded",
}


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(ts: dt.datetime | None = None) -> str:
    return (ts or utc_now()).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def state_dir() -> Path:
    # A set-but-empty (or whitespace-only) GOALFLIGHT_STATE_DIR must fall back to
    # DEFAULT_STATE_DIR, NOT resolve to cwd: os.environ.get(key, default) returns ""
    # when the key is present-but-empty, and Path("").expanduser() == Path(".") (cwd),
    # which scatters capacity.json / capacity.lock into the current directory.
    raw = os.environ.get("GOALFLIGHT_STATE_DIR", "").strip()
    path = Path(raw or DEFAULT_STATE_DIR).expanduser()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def state_path() -> Path:
    return state_dir() / "capacity.json"


def lock_path() -> Path:
    return state_dir() / "capacity.lock"


class StateLock:
    def __enter__(self):
        lock_path().parent.mkdir(parents=True, exist_ok=True)
        self._fh = lock_path().open("w")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()


def load_state() -> dict:
    path = state_path()
    if not path.exists():
        return {"schema": SCHEMA, "machine_id": machine_id(), "leases": {}, "cooldowns": {}}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        data = {"schema": SCHEMA, "machine_id": machine_id(), "leases": {}, "cooldowns": {}}
    data.setdefault("schema", SCHEMA)
    data.setdefault("machine_id", machine_id())
    data.setdefault("leases", {})
    data.setdefault("cooldowns", {})
    return data


def save_state(data: dict) -> None:
    data["updated_at"] = iso()
    tmp = state_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(state_path())


def machine_id() -> str:
    return f"{socket.gethostname()}:{platform.machine()}"


def run_text(cmd: list[str], timeout: float = 2.0) -> str | None:
    try:
        return subprocess.check_output(
            cmd,
            text=True,
            encoding="utf-8",
            errors="replace",
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        ).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def _windows_ram_mb() -> int:
    """Return physical RAM via GlobalMemoryStatusEx, or 0 if unavailable."""
    if not goalflight_compat.is_windows():  # pragma: no cover - Windows only helper
        return 0
    try:  # pragma: no cover - Windows only
        import ctypes
        from ctypes import wintypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GlobalMemoryStatusEx.argtypes = (ctypes.POINTER(MEMORYSTATUSEX),)
        kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
        if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return 0
        return int(status.ullTotalPhys) // 1024 // 1024
    except Exception:
        return 0


def detect_ram_mb() -> int:
    if goalflight_compat.is_windows():
        return _windows_ram_mb()
    if sys.platform == "darwin":
        out = run_text(["sysctl", "-n", "hw.memsize"])
        if out and out.isdigit():
            return int(out) // 1024 // 1024
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemTotal:"):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    return int(parts[1]) // 1024
    return 0


def detect_tools() -> dict:
    grok = shutil.which("grok") or str(Path.home() / ".grok/bin/grok")
    cursor_agent = shutil.which("cursor-agent") or str(Path.home() / ".local/bin/cursor-agent")
    tools = {
        "codex": bool(shutil.which("codex")),
        "codex-acp": bool(shutil.which("codex-acp")),
        "claude": bool(shutil.which("claude")),
        "claude-code-cli-acp": bool(shutil.which("claude-code-cli-acp")),
        "cursor": bool(shutil.which("cursor")),
        "cursor-agent": Path(cursor_agent).exists() if cursor_agent else False,
        "opencode": bool(shutil.which("opencode")),
        "grok": Path(grok).exists() if grok else False,
    }
    return tools


def operating_cap_for_ram(ram_mb: int, raw_ceiling: int) -> int:
    override = os.environ.get("GOALFLIGHT_CAPACITY_MAX_TOTAL")
    if override:
        try:
            return max(1, min(raw_ceiling, int(override)))
        except ValueError:
            pass
    if ram_mb <= 0:
        tier = 2
    elif ram_mb <= 8 * 1024:
        tier = 1
    elif ram_mb <= 16 * 1024:
        tier = 3
    elif ram_mb <= 32 * 1024:
        tier = 4
    elif ram_mb <= 64 * 1024:
        tier = 6
    else:
        # >64GB: tier == DEFAULT_HARD_CAP (was 16, before that 8). On RAM-rich
        # machines the global cap is NOT a memory bound — the acquire-time RSS
        # budget owns RAM safety, per-pool caps + adaptive walk-back own
        # provider limits — it is a CPU/blast-radius BACKSTOP for the case
        # where many workers run local test suites simultaneously. 16 was two
        # arbitrary constants stacked (it bound multi-controller storms well
        # below the pool sums); raised to 20 on monitored evidence
        # (2026-06-10/11: ~1h through three concurrent review storms, zero
        # rate-pressure, no saturation alerts). Going past 20 means revisiting
        # DEFAULT_HARD_CAP with CPU-aware reasoning, not this ladder.
        # raised 20->32 (2026-06-16, operator-requested): a deep-research build needs
        # agent-count headroom on an 18-CPU / 128GB host where the workers are
        # NETWORK-bound (grok-research/codex API calls, not local test suites), so this
        # CPU/blast-radius backstop can rise. 32 ~= 1.8x cores; the acquire-time RSS
        # budget owns RAM, and per-pool caps + adaptive walk-back + the live
        # rate-pressure monitor own provider limits.
        tier = 32
    return max(1, min(raw_ceiling, tier))


def profile(args: argparse.Namespace | None = None) -> dict:
    ram_mb = getattr(args, "ram_mb", None) or detect_ram_mb()
    reserve_mb = getattr(args, "reserve_mb", None) or DEFAULT_RESERVE_MB
    worst_worker_mb = getattr(args, "worst_worker_mb", None) or DEFAULT_WORST_WORKER_MB
    hard_cap = getattr(args, "hard_cap", None) or DEFAULT_HARD_CAP
    headroom_mb = max(0, ram_mb - reserve_mb)
    raw_ceiling = max(1, min(hard_cap, headroom_mb // worst_worker_mb if worst_worker_mb else 1))
    max_total = getattr(args, "max_total", None)
    if max_total:
        operating_cap = max(1, min(raw_ceiling, max_total))
    else:
        operating_cap = operating_cap_for_ram(ram_mb, raw_ceiling)
    payload = {
        "schema": "goalflight.capacity.profile.v1",
        "machine_id": machine_id(),
        "ram_mb": ram_mb,
        "cpu_count": os.cpu_count() or 0,
        "controller_reserve_mb": reserve_mb,
        "worst_case_worker_mb": worst_worker_mb,
        "raw_ram_ceiling": raw_ceiling,
        "operating_cap": operating_cap,
        "hard_cap": hard_cap,
        "agent_caps": DEFAULT_AGENT_CAPS,
        "agent_rss_mb": AGENT_RSS_MB,
        "tools": detect_tools(),
    }
    if (
        goalflight_compat.is_windows()
        and ram_mb <= 0
        and not max_total
        and not os.environ.get("GOALFLIGHT_CAPACITY_MAX_TOTAL")
        and operating_cap == 1
    ):
        payload["warnings"] = [
            "RAM probe unavailable on Windows -> dispatch capped at 1 "
            "(set GOALFLIGHT_CAPACITY_MAX_TOTAL to override)"
        ]
    return payload


def _lease_pids_dead(lease: dict) -> bool:
    """True only when BOTH the worker and orchestrator pids are gone.

    A lease whose tracked processes are all dead is genuinely reclaimable;
    one with a live pid is still consuming RAM and must not be evicted by a
    clock-only TTL check (capacity.json is shared across sibling projects, so a
    TTL eviction here would over-subscribe the machine while the lease is LIVE).
    """
    return not pid_alive(lease.get("worker_pid")) and not pid_alive(
        lease.get("controller_pid")
    )


def prune_state(data: dict) -> None:
    now = utc_now()
    leases = data.get("leases", {})
    for lease_id in list(leases):
        lease = leases[lease_id]
        expires_at = parse_iso(lease.get("expires_at"))
        # TTL expiry is gated on liveness: only flip a past-TTL lease to
        # "expired" when its worker AND orchestrator pids are both dead. A LIVE
        # lease past its TTL is kept and left to liveness-based reclaim
        # (cmd_release-stale / stale_active_leases), so a long-running worker in
        # a sibling project is never evicted out from under itself.
        if expires_at and expires_at < now and _lease_pids_dead(lease):
            lease["state"] = "expired"
            lease["ended_at"] = lease.get("ended_at") or iso()
        terminal_at = parse_iso(lease.get("released_at") or lease.get("ended_at"))
        if lease.get("state") in TERMINAL_LEASE_STATES and terminal_at:
            if now - terminal_at < dt.timedelta(hours=24):
                continue
            leases.pop(lease_id, None)
        elif lease.get("state") in TERMINAL_LEASE_STATES:
            leases.pop(lease_id, None)
    cooldowns = data.get("cooldowns", {})
    for agent in list(cooldowns):
        until = parse_iso(cooldowns[agent].get("until"))
        if until and until < now:
            cooldowns.pop(agent, None)


def extend_active_lease_expiry(lease_id: str | None, seconds: float) -> bool:
    """Move an active lease expiry forward after detected system sleep."""
    if not lease_id or seconds <= 0:
        return False
    with StateLock():
        data = load_state()
        lease = data.get("leases", {}).get(lease_id)
        if not lease or lease.get("state") != "active":
            return False
        expires_at = parse_iso(lease.get("expires_at"))
        if expires_at is None:
            return False
        lease["expires_at"] = iso(expires_at + dt.timedelta(seconds=seconds))
        lease["sleep_pause_extended_s"] = round(
            float(lease.get("sleep_pause_extended_s") or 0.0) + seconds,
            3,
        )
        save_state(data)
        return True


def detach_lease_to_worker(lease_id: str | None, worker_pid: int, reason: object) -> bool:
    """Re-parent a detached-worker lease from launcher pid to worker pid."""
    if not lease_id or not worker_pid:
        return False
    with StateLock():
        data = load_state()
        lease = data.get("leases", {}).get(lease_id)
        if not lease:
            return False
        lease["worker_pid"] = worker_pid
        lease.setdefault("detached_controller_pid", lease.get("controller_pid"))
        lease["controller_pid"] = worker_pid
        lease["detached_at"] = iso()
        lease["detached_reason"] = reason
        save_state(data)
        return True


# Bash-tail + dispatch presets that share one engine/provider concurrency budget.
AGENT_CAP_POOL: dict[str, str] = {
    "grok-code": "grok",
    "grok-research": "grok",
    "grok-acp": "grok",
    "grok-bash-tail": "grok",
}


def normalize_agent(agent: str) -> str:
    return agent.strip().lower()


def cap_pool(agent: str) -> str:
    """Map agent label to the shared capacity pool key (engine/provider budget)."""
    agent = normalize_agent(agent)
    return AGENT_CAP_POOL.get(agent, agent)


def active_leases(data: dict) -> list[dict]:
    return [lease for lease in data.get("leases", {}).values() if lease.get("state") == "active"]


def cooldown_for(data: dict, agent: str) -> dict | None:
    cooldowns = data.get("cooldowns", {})
    return cooldowns.get(agent) or cooldowns.get(agent.split("-")[0])


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _rate_pressure_window_seconds(args: argparse.Namespace | None = None) -> int:
    value = getattr(args, "rate_pressure_window_s", None) if args is not None else None
    if value is None:
        value = os.environ.get("GOALFLIGHT_RATE_PRESSURE_WINDOW_SECONDS")
    return _positive_int(value, DEFAULT_RATE_PRESSURE_WINDOW_SECONDS)


def _rate_pressure_threshold(args: argparse.Namespace | None = None) -> int:
    value = getattr(args, "rate_pressure_threshold", None) if args is not None else None
    if value is None:
        value = os.environ.get("GOALFLIGHT_RATE_PRESSURE_THRESHOLD")
    return _positive_int(value, DEFAULT_RATE_PRESSURE_THRESHOLD)


def current_rate_pressure(args: argparse.Namespace | None = None) -> dict:
    """Return the transient rate-pressure recommendation for this state dir.

    This is deliberately read-only: recent dispatch-ledger failures are the
    cooldown source, and the recommendation disappears as those records age out
    of the rolling window. No permanent caps or capacity state are mutated.
    """
    window_seconds = _rate_pressure_window_seconds(args)
    threshold = _rate_pressure_threshold(args)
    try:
        billing = goalflight_rate_pressure.load_billing_accounts()
        pool_map = goalflight_rate_pressure.agent_limit_pool_map(billing)
        records = goalflight_rate_pressure.collect_records(state_dir())
        pressure = goalflight_rate_pressure.pressure_per_provider(
            records,
            window_seconds=window_seconds,
            pool_map=pool_map,
        )
        payload = goalflight_rate_pressure.recommend(
            pressure,
            dict(DEFAULT_AGENT_CAPS),
            threshold=threshold,
            pool_map=pool_map,
        )
        payload["state_dir"] = str(state_dir())
        payload["window_seconds"] = window_seconds
        payload["records_examined"] = len(records)
        payload["limit_pool_map_loaded"] = bool(pool_map)
        return payload
    except Exception as exc:  # pragma: no cover - defensive status surface
        return {
            "schema": goalflight_rate_pressure.SCHEMA,
            "threshold": threshold,
            "window_seconds": window_seconds,
            "providers_under_pressure": [],
            "providers_observed": [],
            "budget_keys_observed": [],
            "error": f"{type(exc).__name__}: {exc}",
        }


def adaptive_agent_cap(agent: str, base_agent_cap: int, pressure: dict | None = None) -> tuple[int, dict | None]:
    pressure = pressure if pressure is not None else current_rate_pressure()
    agent = normalize_agent(agent)
    pool = cap_pool(agent)
    for entry in pressure.get("providers_under_pressure") or []:
        labels = [normalize_agent(str(label)) for label in entry.get("labels") or []]
        if agent not in labels and pool not in labels:
            continue
        recommended_caps = entry.get("recommended_caps") or {}
        recommended = recommended_caps.get(agent)
        if recommended is None and pool != agent:
            recommended = recommended_caps.get(pool)
        if recommended is None:
            continue
        effective_cap = max(1, min(base_agent_cap, _positive_int(recommended, base_agent_cap)))
        if effective_cap >= base_agent_cap:
            continue
        detail = {
            "agent": agent,
            "scope": entry.get("scope"),
            "provider": entry.get("provider"),
            "budget_key": entry.get("budget_key"),
            "limit_pool_id": entry.get("limit_pool_id"),
            "count": entry.get("count"),
            "threshold": pressure.get("threshold"),
            "window_seconds": pressure.get("window_seconds"),
            "base_agent_cap": base_agent_cap,
            "effective_agent_cap": effective_cap,
            "recommended_caps": recommended_caps,
            "fallback_providers": entry.get("fallback_providers") or [],
        }
        return effective_cap, detail
    return base_agent_cap, None


def rate_pressure_warnings(pressure: dict | None, limit: int = 5) -> list[str]:
    if not pressure:
        return []
    warnings: list[str] = []
    threshold = pressure.get("threshold")
    window = pressure.get("window_seconds")
    for entry in (pressure.get("providers_under_pressure") or [])[:limit]:
        caps = []
        for label, cap in sorted((entry.get("recommended_caps") or {}).items()):
            current = (entry.get("current_caps") or {}).get(label)
            caps.append(f"{label} {current}->{cap}" if current is not None else f"{label}->{cap}")
        if entry.get("scope") == "agent":
            subject = entry.get("budget_key") or "unknown"
        else:
            subject = entry.get("provider") or entry.get("budget_key") or "unknown"
        warnings.append(
            "adaptive rate pressure "
            f"{subject}: count={entry.get('count')}/{threshold} window={window}s; "
            f"effective caps {', '.join(caps) or 'n/a'}; new dispatches wait"
        )
    if pressure.get("error"):
        warnings.append(f"adaptive rate pressure unavailable: {pressure.get('error')}")
    return warnings


def cmd_profile(args: argparse.Namespace) -> int:
    payload = profile(args)
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"capacity: ram={payload['ram_mb']}MB raw={payload['raw_ram_ceiling']} operating={payload['operating_cap']}")
        print(f"tools: {', '.join(k for k, v in payload['tools'].items() if v) or 'none'}")
        for warning in payload.get("warnings") or []:
            print(f"warning: {warning}")
    return 0


def cmd_acquire(args: argparse.Namespace) -> int:
    agent = normalize_agent(args.agent)
    prof = profile(args)
    rss_mb = args.mem_mb or AGENT_RSS_MB.get(agent, DEFAULT_WORST_WORKER_MB)
    with StateLock():
        data = load_state()
        prune_state(data)
        cooldown = cooldown_for(data, agent)
        if cooldown:
            payload = {
                "decision": "wait",
                "reason": f"cooldown:{cooldown.get('reason', 'unspecified')}",
                "agent": agent,
                "retry_after_s": max(0, int((parse_iso(cooldown.get("until")) - utc_now()).total_seconds())) if parse_iso(cooldown.get("until")) else None,
                "cooldown": cooldown,
            }
            save_state(data)
            print(json.dumps(payload, sort_keys=True))
            return 2

        leases = active_leases(data)
        max_total = args.max_total or prof["operating_cap"]
        pool = cap_pool(agent)
        base_agent_cap = args.agent_cap or DEFAULT_AGENT_CAPS.get(pool, DEFAULT_AGENT_CAPS.get(agent, 2))
        pressure = current_rate_pressure(args)
        agent_cap, adaptive_pressure = adaptive_agent_cap(agent, base_agent_cap, pressure)
        priority = (getattr(args, "priority", None) or "normal").strip().lower()
        if priority not in PRIORITY_LANES:
            save_state(data)  # persist the prune_state() above (hygiene)
            print(json.dumps({"decision": "error", "reason": f"unknown priority {priority!r}; choose one of {PRIORITY_LANES}"}, sort_keys=True))
            return 2
        # Lane-adjusted ceilings. Global critical borrow never exceeds the RAM
        # raw ceiling; pool critical borrow yields to active rate-pressure.
        lane_max_total = max_total
        lane_agent_cap = agent_cap
        if priority == "bulk":
            lane_max_total = max(1, max_total - BULK_GLOBAL_RESERVE)
            lane_agent_cap = max(1, agent_cap - BULK_POOL_RESERVE)
        elif priority == "critical":
            lane_max_total = min(max_total + CRITICAL_GLOBAL_BORROW, prof["raw_ram_ceiling"])
            if adaptive_pressure is None:
                lane_agent_cap = agent_cap + CRITICAL_POOL_BORROW
        agent_count = sum(
            1 for lease in leases if cap_pool(normalize_agent(lease.get("agent", ""))) == pool
        )
        total_rss = sum(int(lease.get("mem_mb") or 0) for lease in leases)
        if len(leases) >= lane_max_total:
            payload = {
                "decision": "wait",
                "reason": "machine_worker_cap",
                "active": len(leases),
                "max_total": max_total,
                "priority": priority,
                "lane_max_total": lane_max_total,
            }
            save_state(data)
            print(json.dumps(payload, sort_keys=True))
            return 2
        if agent_count >= lane_agent_cap:
            reason = "adaptive_rate_pressure" if adaptive_pressure else "agent_worker_cap"
            payload = {
                "decision": "wait",
                "reason": reason,
                "agent": agent,
                "active": agent_count,
                "agent_cap": agent_cap,
                "base_agent_cap": base_agent_cap,
                "priority": priority,
                "lane_agent_cap": lane_agent_cap,
            }
            if adaptive_pressure:
                payload["adaptive_rate_pressure"] = adaptive_pressure
            save_state(data)
            print(json.dumps(payload, sort_keys=True))
            return 2
        if prof["ram_mb"] and total_rss + rss_mb > max(0, prof["ram_mb"] - prof["controller_reserve_mb"]):
            # RAM safety binds ALL lanes — critical cannot borrow past the RSS budget.
            payload = {"decision": "wait", "reason": "rss_budget", "active_rss_mb": total_rss, "request_mem_mb": rss_mb, "priority": priority}
            save_state(data)
            print(json.dumps(payload, sort_keys=True))
            return 2

        lease_id = args.lease_id or str(uuid.uuid4())
        ttl = dt.timedelta(seconds=args.ttl_s)
        lease = {
            "lease_id": lease_id,
            "dispatch_id": args.dispatch_id,
            "prompt_id": args.prompt_id,
            "agent": agent,
            "project_root": args.project_root,
            "worker_cwd": getattr(args, "worker_cwd", None),
            "worktree_path": getattr(args, "worktree_path", None),
            "controller_pid": args.controller_pid or os.getpid(),
            "worker_pid": args.worker_pid,
            "mem_mb": rss_mb,
            "priority": priority,
            "state": "active",
            "started_at": iso(),
            "expires_at": iso(utc_now() + ttl),
        }
        data.setdefault("leases", {})[lease_id] = lease
        save_state(data)
    print(json.dumps({"decision": "allow", "lease": lease, "profile": prof}, sort_keys=True))
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    with StateLock():
        data = load_state()
        lease = data.get("leases", {}).get(args.lease_id)
        if not lease:
            print(json.dumps({"ok": False, "reason": "missing_lease", "lease_id": args.lease_id}, sort_keys=True))
            return 1
        lease["state"] = args.state
        lease["released_at"] = iso()
        if args.reason:
            lease["reason"] = args.reason
        if args.keep:
            save_state(data)
        else:
            data.get("leases", {}).pop(args.lease_id, None)
            save_state(data)
    print(json.dumps({"ok": True, "lease_id": args.lease_id, "state": args.state}, sort_keys=True))
    return 0


def cmd_cooldown(args: argparse.Namespace) -> int:
    agent = normalize_agent(args.agent)
    with StateLock():
        data = load_state()
        prune_state(data)
        if args.action == "clear":
            data.get("cooldowns", {}).pop(agent, None)
            save_state(data)
            print(json.dumps({"ok": True, "agent": agent, "action": "clear"}, sort_keys=True))
            return 0
        until = utc_now() + dt.timedelta(seconds=args.seconds)
        data.setdefault("cooldowns", {})[agent] = {
            "agent": agent,
            "reason": args.reason,
            "until": iso(until),
            "recorded_at": iso(),
        }
        save_state(data)
    print(json.dumps({"ok": True, "agent": agent, "until": iso(until), "reason": args.reason}, sort_keys=True))
    return 0


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    return goalflight_compat.pid_alive(pid)


def stale_active_leases(data: dict) -> list[dict]:
    """Active leases whose orchestrator or worker PID is no longer running."""
    stale: list[dict] = []
    for lease in active_leases(data):
        controller_pid = lease.get("controller_pid")
        worker_pid = lease.get("worker_pid")
        if worker_pid is not None:
            if pid_alive(worker_pid):
                continue
            stale.append(lease)
            continue
        if not pid_alive(controller_pid):
            stale.append(lease)
    return stale


def cmd_release_stale(args: argparse.Namespace) -> int:
    released: list[str] = []
    with StateLock():
        data = load_state()
        prune_state(data)
        for lease in stale_active_leases(data):
            lease_id = lease.get("lease_id")
            if not lease_id:
                continue
            entry = data.get("leases", {}).get(lease_id)
            if not entry:
                continue
            entry["state"] = args.state
            entry["released_at"] = iso()
            entry["reason"] = args.reason
            if not args.keep:
                data.get("leases", {}).pop(lease_id, None)
            released.append(str(lease_id))
        save_state(data)
    payload = {"ok": True, "released": released, "count": len(released)}
    print(json.dumps(payload, sort_keys=True))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    # Status is a READ. It computes a pruned VIEW for display but never
    # persists, so a frequent status poll can't evict a live lease (capacity.json
    # is shared across sibling projects; persisting a prune here would let any
    # poller race-flip another project's lease). Active-lease reclaim is the
    # job of `release-stale`, which is liveness-gated by design.
    with StateLock():
        data = load_state()
    prune_state(data)
    pressure = current_rate_pressure(args)
    payload = {
        "schema": SCHEMA,
        "profile": profile(args),
        "state": data,
        "active": active_leases(data),
        "rate_pressure": pressure,
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True))
        return 0
    prof = payload["profile"]
    print(f"capacity: active={len(payload['active'])}/{prof['operating_cap']} raw={prof['raw_ram_ceiling']} ram={prof['ram_mb']}MB")
    for lease in payload["active"]:
        prio = lease.get("priority")
        prio_part = f" prio={prio}" if prio and prio != "normal" else ""
        print(f"- {lease['lease_id']} agent={lease['agent']} dispatch={lease.get('dispatch_id')} mem={lease.get('mem_mb')}MB{prio_part}")
    if data.get("cooldowns"):
        print("cooldowns:")
        for cooldown in data["cooldowns"].values():
            print(f"- {cooldown['agent']}: {cooldown.get('reason')} until {cooldown.get('until')}")
    for warning in rate_pressure_warnings(pressure):
        print(f"warning: {warning}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="goal-flight machine capacity coordinator")
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--ram-mb", type=int)
    parent.add_argument("--reserve-mb", type=int, default=DEFAULT_RESERVE_MB)
    parent.add_argument("--worst-worker-mb", type=int, default=DEFAULT_WORST_WORKER_MB)
    parent.add_argument("--hard-cap", type=int, default=DEFAULT_HARD_CAP)
    parent.add_argument("--max-total", type=int)
    parent.add_argument("--rate-pressure-window-s", type=int)
    parent.add_argument("--rate-pressure-threshold", type=int)

    sub = parser.add_subparsers(dest="cmd", required=True)
    prof = sub.add_parser("profile", parents=[parent])
    prof.add_argument("--json", action="store_true")
    prof.set_defaults(func=cmd_profile)

    acq = sub.add_parser("acquire", parents=[parent])
    acq.add_argument("--agent", required=True)
    acq.add_argument("--dispatch-id")
    acq.add_argument("--prompt-id")
    acq.add_argument("--project-root")
    acq.add_argument("--worker-cwd")
    acq.add_argument("--worktree-path")
    acq.add_argument("--controller-pid", type=int)
    acq.add_argument("--worker-pid", type=int)
    acq.add_argument("--lease-id")
    acq.add_argument("--mem-mb", type=int)
    acq.add_argument("--agent-cap", type=int)
    acq.add_argument("--priority", choices=list(PRIORITY_LANES), default="normal",
                     help="capacity lane: bulk reserves headroom for others (review storms); "
                          "critical may borrow beyond the operating/pool cap (fix dispatches)")
    acq.add_argument("--ttl-s", type=int, default=8 * 60 * 60)
    acq.set_defaults(func=cmd_acquire)

    rel = sub.add_parser("release")
    rel.add_argument("--lease-id", required=True)
    rel.add_argument("--state", default="released")
    rel.add_argument("--reason")
    rel.add_argument("--keep", action="store_true")
    rel.set_defaults(func=cmd_release)

    rel_stale = sub.add_parser("release-stale")
    rel_stale.add_argument("--state", default="expired")
    rel_stale.add_argument("--reason", default="stale_controller")
    rel_stale.add_argument("--keep", action="store_true")
    rel_stale.set_defaults(func=cmd_release_stale)

    cool = sub.add_parser("cooldown")
    cool.add_argument("action", choices=["set", "clear"])
    cool.add_argument("--agent", required=True)
    cool.add_argument("--seconds", type=int, default=3600)
    cool.add_argument("--reason", default="rate_limit")
    cool.set_defaults(func=cmd_cooldown)

    stat = sub.add_parser("status", parents=[parent])
    stat.add_argument("--json", action="store_true")
    stat.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
