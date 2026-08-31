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
from goalflight_agent_limits import (
    AGENT_CAP_POOL,
    AGENT_RSS_MB,
    DEFAULT_AGENT_CAPS,
    LEGACY_AGENT_HANDLES,
    cap_pool,
    local_hard_cap,
    local_operating_total,
    normalize_agent,
)
import goalflight_dispatch_states as dispatch_states
import goalflight_quota_stuck
import goalflight_rate_pressure
from goalflight_liveness import active_monotonic

SCHEMA = "goalflight.capacity.v1"


DEFAULT_STATE_DIR = goalflight_compat.resolve_state_dir()
DEFAULT_RESERVE_MB = 2048
DEFAULT_WORST_WORKER_MB = 1200
# DEFAULT_HARD_CAP is the committed generic baseline raw_ceiling INPUT;
# operating_cap = min(raw_ceiling, tier|conf|env), and raw_ceiling itself =
# min(this, headroom_mb // worst_worker_mb), so RAM and the acquire-time RSS
# budget continue to bound real concurrency. A specific big box that wants a
# higher ceiling sets "hard_cap" in its gitignored capacity.local.json rather
# than editing this tracked default (which would export one machine's tuning to
# every user of the skill). local_hard_cap() returns the committed baseline when
# no conf is present.
DEFAULT_HARD_CAP = local_hard_cap(40)
DEFAULT_RATE_PRESSURE_WINDOW_SECONDS = 600
DEFAULT_RATE_PRESSURE_THRESHOLD = 3
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

# A watcher that cannot safely dispose an unresolved process group keeps its
# slot accounted.  The historical PGID is safe for this non-destructive
# existence check (a recycled group can only retain capacity), but the hold is
# bounded so PID/PGID reuse cannot consume a slot forever.
INDETERMINATE_LIVE_RETENTION_S = 7200
INDETERMINATE_LIVE_REASON = "liveness_indeterminate_worker_live"
PROCESS_IDENTITY_SCHEMA = "start-token-v1"


CAPACITY_STATE_UNREADABLE = "capacity_state_unreadable"


class CapacityStateUnreadable(Exception):
    """Raised when capacity.json cannot be read or parsed.

    This is UNKNOWN, never a measured empty lease set. Admission must refuse.
    """


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

LEASE_ONLY_TERMINAL_STATES = frozenset(
    {
        "expired",
        # Legacy 0.4.3 terminal state. Current ACP oversized frames drop and
        # continue; keep this so old lease records still prune.
        "result_too_large",
    }
)
TERMINAL_LEASE_STATES = dispatch_states.TERMINAL_STATES | LEASE_ONLY_TERMINAL_STATES


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
    path = goalflight_compat.resolve_state_dir()
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


def _empty_state() -> dict:
    return {"schema": SCHEMA, "machine_id": machine_id(), "leases": {}, "cooldowns": {}}


def load_state() -> dict:
    """Load capacity.json.

    A missing file is a measured empty (first use). An unreadable or corrupt
    file is UNKNOWN: raise ``CapacityStateUnreadable`` instead of returning
    zero leases.

    This is the OPPOSITE of the queue/journal keep-direction, and the
    contrast is deliberate. For deletion/terminalization, unknown must keep
    the work — treating unreadability as absence destroys live envelopes and
    journals. For admission, unknown must refuse — treating unreadability as
    zero leases over-commits the machine at exactly the moment it is
    unhealthy.

    Retry/backoff is not widened here. ``acquire_with_wait`` already polls
    ``decision=wait`` at ``CAPACITY_WAIT_POLL_S`` plus jitter until the lane
    wait budget expires (defaults in ``CAPACITY_WAIT_DEFAULTS_S``). A flaky
    read therefore retries on the existing budget; a persistent unreadable
    file then surfaces as ``reason=capacity_state_unreadable``, distinct from
    ``machine_worker_cap`` / ``agent_worker_cap``, so it cannot be mistaken
    for a genuine cap-reached hold (t-068).
    """
    path = state_path()
    # ``Path.exists()`` returns False on OSError, collapsing parent-unreadable
    # into absent. Only FileNotFoundError is evidence the file is gone.
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty_state()
    except OSError as exc:
        raise CapacityStateUnreadable(
            f"capacity state unreadable: {type(exc).__name__}: {path}"
        ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CapacityStateUnreadable(
            f"capacity state corrupt: JSONDecodeError: {path}"
        ) from exc
    if not isinstance(data, dict):
        raise CapacityStateUnreadable(f"capacity state is not an object: {path}")
    # Only an absent file is authoritative measured-empty. An existing object
    # without its lease map is incomplete state, not permission to admit.
    if "leases" not in data or not isinstance(data.get("leases"), dict):
        raise CapacityStateUnreadable(
            f"capacity state has no readable lease map: {path}"
        )
    data.setdefault("schema", SCHEMA)
    data.setdefault("machine_id", machine_id())
    data.setdefault("cooldowns", {})
    if not isinstance(data.get("cooldowns"), dict):
        raise CapacityStateUnreadable(
            f"capacity state has no readable cooldown map: {path}"
        )
    return data


def unreadable_admission_payload(exc: CapacityStateUnreadable) -> dict:
    """Wait payload for an unreadable capacity read. Not cap-reached."""
    return {
        "decision": "wait",
        "reason": CAPACITY_STATE_UNREADABLE,
        "error": str(exc),
        "measured": False,
        "retry_after_s": int(CAPACITY_WAIT_POLL_S),
    }


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
    # Persistent per-machine operating cap from capacity.local.json. Behaves
    # exactly like GOALFLIGHT_CAPACITY_MAX_TOTAL but durable; the explicit env
    # var above (and CLI --max-total in profile()) still take precedence.
    conf_total = local_operating_total()
    if conf_total:
        return max(1, min(raw_ceiling, conf_total))
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


def _process_group_liveness(pgid: object) -> bool | None:
    """Return process-group liveness; None means the safe probe is unavailable."""
    try:
        parsed_pgid = int(pgid)
    except (TypeError, ValueError):
        return None
    if parsed_pgid <= 1 or not hasattr(os, "killpg"):
        return None
    try:
        os.killpg(parsed_pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return None


def _indeterminate_retention_open(
    lease: dict,
    *,
    now: dt.datetime | None = None,
) -> bool:
    """True while an unprobeable holder is still inside the 7200s window.

    Premise: a probe of None is not death (must not reclaim a live worker)
    and not life-forever (EPERM / foreign-pid reuse must not park a slot).
    Confirmed-live never consults this helper.

    Arithmetic: reuse the persisted watcher stamp ``accounted_live_until``
    when present. Otherwise the origin is ``started_at`` (falling back to
    ``expires_at`` only if the claim clock is missing) plus
    ``INDETERMINATE_LIVE_RETENTION_S``. Those clocks are durable, so
    ``prune_state``'s non-persisted status VIEW cannot refresh the window
    the way a first-seen-at-now stamp would.

    Foreign-pid-reuse cost: the slot is parked until claim+7200s (2h), not
    claim+TTL+7200s (~10h at the 8h default), and
    ``extend_active_lease_expiry`` cannot push the bound because it only
    moves ``expires_at``.

    Live-worker case: an identity-qualified matching PID holds until death;
    this bound is never applied. With no lease clock, first observation writes
    ``liveness_unknown_since`` into the lease mapping. Mutating callers persist
    that clock, giving unknown a full bounded window without refreshing it on
    every reconciliation.
    """
    until = parse_iso(lease.get("accounted_live_until"))
    if until is None:
        origin = (
            parse_iso(lease.get("started_at"))
            or parse_iso(lease.get("expires_at"))
            or parse_iso(lease.get("liveness_unknown_since"))
        )
        if origin is None:
            origin = now or utc_now()
            lease["liveness_unknown_since"] = iso(origin)
        until = origin + dt.timedelta(seconds=float(INDETERMINATE_LIVE_RETENTION_S))
    return (now or utc_now()) < until


def _probe_pid_liveness(pid: object) -> bool | None:
    """Tri-state pid probe for reclaim. Missing/invalid pid is confirmed dead."""
    if not pid:
        return False
    return goalflight_compat.pid_liveness(pid)


def _process_start_identity(pid: object) -> dict | None:
    """Capture the fine process-generation token used to reject PID reuse."""
    try:
        parsed_pid = int(pid)
    except (TypeError, ValueError):
        return None
    if parsed_pid <= 0:
        return None
    return goalflight_compat.process_start_identity(parsed_pid)


def _lease_identity_for_pid(lease: dict, pid: object) -> dict | None:
    try:
        parsed_pid = int(pid)
    except (TypeError, ValueError):
        return None
    for role in ("worker", "controller", "claimant"):
        try:
            role_pid = int(lease.get(f"{role}_pid"))
        except (TypeError, ValueError):
            continue
        identity = lease.get(f"{role}_identity")
        if role_pid == parsed_pid and isinstance(identity, dict):
            return identity
    return None


def _pid_generation_matches(pid: object, lease: dict) -> bool | None:
    """Match a live PID to its recorded start token; None means unavailable."""
    expected = _lease_identity_for_pid(lease, pid)
    expected_token = expected.get("start_token") if expected else None
    current = _process_start_identity(pid)
    current_token = current.get("start_token") if current else None
    if not expected_token or not current_token:
        return None
    return current_token == expected_token


def _pid_holds_capacity(
    pid: object,
    lease: dict,
    *,
    now: dt.datetime | None = None,
) -> bool:
    """Whether this pid still protects the lease against reclaim.

    Confirmed live holds until death (boolean ``pid_alive`` stays conservative
    for kill/reap). A live PID is not an unknown probe: missing start-token
    evidence must not authorise expiry, because that reclaims a process we
    measured alive. Without a stored token we also cannot prove PID reuse, so
    we hold until death. Acquire/attach persist the token so reuse is a
    mismatch (False) instead of this hold. Confirmed dead does not hold.
    Indeterminate (probe None) holds only inside the 7200s window derived by
    ``_indeterminate_retention_open`` so EPERM/foreign-pid reuse cannot park a
    slot for that process's lifetime.
    """
    live = _probe_pid_liveness(pid)
    if live is True:
        generation_matches = _pid_generation_matches(pid, lease)
        if generation_matches is False:
            return False
        return True
    if live is False:
        return False
    return _indeterminate_retention_open(lease, now=now)


def retained_live_scope_holds_capacity(
    lease: dict,
    *,
    now: dt.datetime | None = None,
) -> bool:
    """Keep an unresolved worker scope accounted until death is confirmed.

    A confirmed-live group remains capacity-bearing after ``accounted_live_until``
    (that stamp is still a recheck horizon while we can see the group). An
    unprobeable group cannot use the horizon as infinite protection: the same
    7200s constant becomes a reclaim deadline. Only a negative process-group
    probe, or an elapsed indeterminate window, releases the retained scope.
    """
    if lease.get("reason") != INDETERMINATE_LIVE_REASON:
        return False
    # A historical PGID is never signaled here; reuse can delay capacity, not
    # hurt a process. Confirmed-live still wins over an elapsed stamp.
    group_alive = _process_group_liveness(lease.get("accounted_live_pgid"))
    if group_alive is True:
        return True
    if group_alive is False:
        return False
    return _indeterminate_retention_open(lease, now=now)


def retain_indeterminate_live_lease(
    lease_id: str,
    *,
    pgid: object,
    retention_s: float = INDETERMINATE_LIVE_RETENTION_S,
) -> dict:
    """Durably account an unresolved group and record its next recheck horizon."""
    with StateLock():
        data = load_state()
        lease = data.get("leases", {}).get(str(lease_id))
        if not isinstance(lease, dict):
            raise KeyError(f"missing capacity lease {lease_id}")
        now = utc_now()
        lease["state"] = "active"
        lease["reason"] = INDETERMINATE_LIVE_REASON
        lease["accounted_live_at"] = iso(now)
        lease["accounted_live_until"] = iso(
            now + dt.timedelta(seconds=max(0.0, float(retention_s)))
        )
        try:
            parsed_pgid = int(pgid)
        except (TypeError, ValueError):
            parsed_pgid = 0
        if parsed_pgid > 1:
            lease["accounted_live_pgid"] = parsed_pgid
        save_state(data)
        return dict(lease)


def attached_worker_group_holds_capacity(lease: dict) -> bool:
    """Keep an attached worker accounted until its full group is confirmed gone."""
    if not lease.get("worker_pgid"):
        return False
    worker_pid = lease.get("worker_pid")
    if (
        _probe_pid_liveness(worker_pid) is True
        and _pid_generation_matches(worker_pid, lease) is False
    ):
        # The live process group belongs to a reused leader generation, not the
        # recorded worker. Group existence cannot overrule a start-token mismatch.
        return False
    # Confirmed-live group: hold (descendants may outlive a dead leader). If
    # identity cannot be established, we cannot tell original from reuse;
    # expiry would reclaim a live original worker. Attach persists the token
    # so reuse hits the mismatch short-circuit above instead of this hold.
    # Unprobeable group: the same 7200s bound as pid reclaim, not infinite.
    group_alive = _process_group_liveness(lease.get("worker_pgid"))
    if group_alive is True:
        return True
    if group_alive is None:
        return _indeterminate_retention_open(lease)
    # Group is confirmed dead. Missing identity used to skip the PID probe and
    # park the slot until started_at+7200s even when the worker was also
    # confirmed dead. Consult the PID: confirmed-dead reclaims; confirmed-live
    # holds (reuse without a token is indistinguishable from the original);
    # unknown is bounded.
    return _pid_holds_capacity(worker_pid, lease)


def _lease_pids_dead(lease: dict) -> bool:
    """True only when every process that can hold the lease is gone.

    A lease whose tracked processes are all dead is genuinely reclaimable;
    one with a live pid is still consuming RAM and must not be evicted by a
    clock-only TTL check (capacity.json is shared across sibling projects, so a
    TTL eviction here would over-subscribe the machine while the lease is LIVE).
    An indeterminate probe is not treated as dead immediately (that reclaims a
    live worker on EPERM) and not as live forever (that parks the slot for a
    foreign pid's lifetime). Consult pid_liveness; None is bounded.
    """
    if (
        retained_live_scope_holds_capacity(lease)
        or attached_worker_group_holds_capacity(lease)
    ):
        return False
    worker_pid = lease.get("worker_pid")
    claimant_pid = lease.get("claimant_pid") if worker_pid is None else None
    return (
        not _pid_holds_capacity(worker_pid, lease)
        and not _pid_holds_capacity(lease.get("controller_pid"), lease)
        and not _pid_holds_capacity(claimant_pid, lease)
    )


def prune_state(data: dict) -> None:
    now = utc_now()
    leases = data.get("leases", {})
    for lease_id in list(leases):
        lease = leases[lease_id]
        expires_at = parse_iso(lease.get("expires_at"))
        # TTL expiry is gated on liveness: only flip a past-TTL lease to
        # "expired" when its worker, controller, and pre-attach claimant are all
        # dead. A LIVE lease past its TTL is kept and left to liveness-based
        # reclaim (cmd_release-stale / stale_active_leases), so a long-running
        # worker in a sibling project is never evicted out from under itself.
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


def record_attached_worker(
    lease: dict,
    worker_pid: int,
    worker_pgid: int | None = None,
) -> None:
    """Write pid/pgid and start token onto an in-memory lease.

    Capture failure leaves identity unset rather than inventing a token. A
    confirmed-live PID/group then holds until death; a confirmed-dead
    PID/group is reclaimable. Expiring because the token is missing would
    reclaim a live worker.
    """
    lease["worker_pid"] = worker_pid
    if worker_pgid and worker_pgid > 1:
        lease["worker_pgid"] = worker_pgid
    identity = _process_start_identity(worker_pid)
    lease["process_identity_schema"] = PROCESS_IDENTITY_SCHEMA
    lease.pop("worker_identity", None)
    if identity is not None:
        lease["worker_identity"] = identity


def attach_worker_to_capacity_lease(
    lease_id: str | None,
    worker_pid: int,
    worker_pgid: int | None = None,
) -> None:
    """Persist worker pid/pgid and start token before RUNNING."""
    if not lease_id:
        return
    with StateLock():
        data = load_state()
        lease = data.get("leases", {}).get(lease_id)
        if lease:
            record_attached_worker(lease, worker_pid, worker_pgid)
            save_state(data)


def detach_lease_to_worker(lease_id: str | None, worker_pid: int, reason: object) -> bool:
    """Make a detached worker's own pid authoritative for lease liveness."""
    if not lease_id or not worker_pid:
        return False
    with StateLock():
        data = load_state()
        lease = data.get("leases", {}).get(lease_id)
        if not lease:
            return False
        worker_identity = _process_start_identity(worker_pid)
        lease["process_identity_schema"] = PROCESS_IDENTITY_SCHEMA
        lease["worker_pid"] = worker_pid
        lease.pop("worker_identity", None)
        if worker_identity is not None:
            lease["worker_identity"] = worker_identity
        lease.setdefault("detached_controller_pid", lease.get("controller_pid"))
        lease["controller_pid"] = worker_pid
        lease.pop("controller_identity", None)
        if worker_identity is not None:
            lease["controller_identity"] = worker_identity
        lease["detached_at"] = iso()
        lease["detached_reason"] = reason
        save_state(data)
        return True


def active_leases(data: dict) -> list[dict]:
    return [lease for lease in data.get("leases", {}).values() if lease.get("state") == "active"]


def launch_slot_budget(
    agent: str | None = None,
    priority: str = "normal",
    args: argparse.Namespace | None = None,
) -> dict:
    """Read-only remaining worker slots. Never persists.

    Fail-closed: unreadable capacity state yields remaining=0 and
    ``unreadable=True``. Lane math matches acquire so drain can ask instead
    of throttling by being slow. Drain itself stays serial (same-task spawn
    is a TOCTOU on the ledger-read completion gate); this budget is the
    operator/report surface and the cap on "should we even try".
    """
    del priority  # acquire-time lane ceilings stay on the child acquire path
    prof = profile(args)
    operating_cap = int(prof["operating_cap"])
    try:
        with StateLock():
            data = load_state()
    except CapacityStateUnreadable as exc:
        return {
            "unreadable": True,
            "reason": str(exc),
            "operating_cap": operating_cap,
            "active": 0,
            "global_remaining": 0,
            "by_pool": {},
            "agent": agent,
            "agent_remaining": 0,
        }
    prune_state(data)
    leases = active_leases(data)
    active = len(leases)
    global_remaining = max(0, operating_cap - active)
    pressure = current_rate_pressure(args)
    pool_active: dict[str, int] = {}
    for lease in leases:
        pool = cap_pool(normalize_agent(str(lease.get("agent") or "")))
        if not pool:
            continue
        pool_active[pool] = pool_active.get(pool, 0) + 1
    by_pool: dict[str, int] = {}
    for pool in set(DEFAULT_AGENT_CAPS) | set(pool_active):
        if not pool:
            continue
        base = DEFAULT_AGENT_CAPS.get(pool, 2)
        effective, _detail = adaptive_agent_cap(pool, base, pressure)
        by_pool[pool] = max(0, int(effective) - int(pool_active.get(pool, 0)))
    agent_remaining = None
    if agent:
        pool = cap_pool(normalize_agent(agent))
        agent_remaining = min(global_remaining, by_pool.get(pool, global_remaining))
    return {
        "unreadable": False,
        "operating_cap": operating_cap,
        "active": active,
        "global_remaining": global_remaining,
        "by_pool": by_pool,
        "agent": agent,
        "agent_remaining": agent_remaining,
    }


def cooldown_for(data: dict, agent: str) -> dict | None:
    cooldowns = data.get("cooldowns", {})
    return cooldowns.get(agent) or cooldowns.get(agent.split("-")[0])


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _rate_pressure_requested_policy(args: argparse.Namespace | None = None) -> tuple[object, object]:
    window_value = getattr(args, "rate_pressure_window_s", None) if args is not None else None
    threshold_value = getattr(args, "rate_pressure_threshold", None) if args is not None else None
    if window_value is None:
        window_value = os.environ.get("GOALFLIGHT_RATE_PRESSURE_WINDOW_SECONDS")
    if threshold_value is None:
        threshold_value = os.environ.get("GOALFLIGHT_RATE_PRESSURE_THRESHOLD")
    return window_value, threshold_value


def _rate_pressure_policy(args: argparse.Namespace | None = None) -> dict:
    # capacity.json is a shared pool across controllers. Per-session windows or
    # thresholds would let two controllers make different dispatch decisions from
    # the same leases/ledger, so v1 refuses overrides and reports the refusal.
    requested_window, requested_threshold = _rate_pressure_requested_policy(args)
    window_seconds = DEFAULT_RATE_PRESSURE_WINDOW_SECONDS
    threshold = DEFAULT_RATE_PRESSURE_THRESHOLD
    warnings: list[str] = []
    parsed_window = _positive_int(requested_window, window_seconds)
    parsed_threshold = _positive_int(requested_threshold, threshold)
    if requested_window not in (None, "") and parsed_window != window_seconds:
        warnings.append(
            "ignored per-session rate-pressure window override "
            f"{requested_window!r}; shared pool uses {window_seconds}s"
        )
    if requested_threshold not in (None, "") and parsed_threshold != threshold:
        warnings.append(
            "ignored per-session rate-pressure threshold override "
            f"{requested_threshold!r}; shared pool uses {threshold}"
        )
    return {
        "window_seconds": window_seconds,
        "threshold": threshold,
        "override_mode": "refuse_per_session",
        "warnings": warnings,
    }


def _rate_pressure_window_seconds(args: argparse.Namespace | None = None) -> int:
    return int(_rate_pressure_policy(args)["window_seconds"])


def _rate_pressure_threshold(args: argparse.Namespace | None = None) -> int:
    return int(_rate_pressure_policy(args)["threshold"])


def current_rate_pressure(args: argparse.Namespace | None = None) -> dict:
    """Return the transient rate-pressure recommendation for this state dir.

    This is deliberately read-only: recent dispatch-ledger failures are the
    cooldown source, and the recommendation disappears as those records age out
    of the rolling window. No permanent caps or capacity state are mutated.
    """
    policy = _rate_pressure_policy(args)
    window_seconds = int(policy["window_seconds"])
    threshold = int(policy["threshold"])
    try:
        billing = goalflight_rate_pressure.load_billing_accounts()
        pool_map = goalflight_rate_pressure.agent_limit_pool_map(billing)
        records = goalflight_rate_pressure.collect_records(state_dir())
        pressure = goalflight_rate_pressure.pressure_per_provider(
            records,
            window_seconds=window_seconds,
            pool_map=pool_map,
        )
        quota_pressure = goalflight_quota_stuck.quota_pressure_per_provider(
            records,
            window_seconds=window_seconds,
            pool_map=pool_map,
        )
        for key, count in quota_pressure.items():
            pressure[key] = max(pressure.get(key, 0), count)
        payload = goalflight_rate_pressure.recommend(
            pressure,
            dict(DEFAULT_AGENT_CAPS),
            threshold=threshold,
            pool_map=pool_map,
        )
        payload = goalflight_quota_stuck.decorate_pressure_payload(
            payload,
            records,
            window_seconds=window_seconds,
            pool_map=pool_map,
        )
        payload["state_dir"] = str(state_dir())
        payload["window_seconds"] = window_seconds
        payload["policy"] = {
            "override_mode": policy["override_mode"],
            "window_seconds": window_seconds,
            "threshold": threshold,
        }
        payload["policy_warnings"] = list(policy["warnings"])
        payload["records_examined"] = len(records)
        payload["limit_pool_map_loaded"] = bool(pool_map)
        return payload
    except Exception as exc:  # pragma: no cover - defensive status surface
        return {
            "schema": goalflight_rate_pressure.SCHEMA,
            "threshold": threshold,
            "window_seconds": window_seconds,
            "policy": {
                "override_mode": policy["override_mode"],
                "window_seconds": window_seconds,
                "threshold": threshold,
            },
            "policy_warnings": list(policy["warnings"]),
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
        if entry.get("scope") == "account":
            # Capacity leases are keyed by agent pool/label and carry no account,
            # so applying account-scoped pressure here would zero machine-global
            # labels shared by every seat of the provider (stopping healthy
            # siblings). account_quota_advisory is currently advisory only —
            # no automated consumer holds or reroutes on it yet.
            continue
        labels = [normalize_agent(str(label)) for label in entry.get("labels") or []]
        if agent not in labels and pool not in labels:
            continue
        recommended_caps = entry.get("effective_caps") or entry.get("recommended_caps") or {}
        recommended = recommended_caps.get(agent)
        if recommended is None and pool != agent:
            recommended = recommended_caps.get(pool)
        if recommended is None:
            continue
        if entry.get("quota_hard_stop") and entry.get("scope") != "agent":
            effective_cap = 0
        else:
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
            "quota_hard_stop": bool(entry.get("quota_hard_stop")),
            "stuck_worker_count": entry.get("stuck_worker_count"),
            "fallback_providers": entry.get("fallback_providers") or [],
        }
        return effective_cap, detail
    return base_agent_cap, None


def rate_pressure_warnings(pressure: dict | None, limit: int = 5) -> list[str]:
    if not pressure:
        return []
    warnings: list[str] = []
    warnings.extend(str(item) for item in pressure.get("policy_warnings") or [])
    threshold = pressure.get("threshold")
    window = pressure.get("window_seconds")
    for entry in (pressure.get("providers_under_pressure") or [])[:limit]:
        caps = []
        account_scoped = entry.get("scope") == "account"
        display_caps = (
            {}
            if account_scoped
            else entry.get("effective_caps") or entry.get("recommended_caps") or {}
        )
        for label, cap in sorted(display_caps.items()):
            current = (entry.get("current_caps") or {}).get(label)
            caps.append(f"{label} {current}->{cap}" if current is not None else f"{label}->{cap}")
        if entry.get("scope") in {"agent", "account"}:
            subject = entry.get("budget_key") or "unknown"
        else:
            subject = entry.get("provider") or entry.get("budget_key") or "unknown"
        if account_scoped:
            # Capacity never holds account scope; wording must stay advisory-only.
            action = (
                "account-lane advisory only, no automated consumer; "
                "label caps unchanged"
            )
        else:
            action = "new dispatches wait"
        warnings.append(
            "adaptive rate pressure "
            f"{subject}: count={entry.get('count')}/{threshold} window={window}s; "
            f"stuck={entry.get('stuck_worker_count', 0)}; "
            f"effective caps {', '.join(caps) or 'n/a'}; {action}"
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
    successor = LEGACY_AGENT_HANDLES.get(agent)
    if successor:
        # INPUT boundary: the retired handle must not acquire capacity. Legacy
        # leases still ACCOUNT against the successor pool via cap_pool(), but no
        # new lease may be taken under the old label.
        print(json.dumps({
            "decision": "error",
            "reason": f"agent {agent!r} is retired; acquire as {successor!r}",
        }, sort_keys=True))
        return 2
    prof = profile(args)
    rss_mb = args.mem_mb or AGENT_RSS_MB.get(agent, DEFAULT_WORST_WORKER_MB)
    with StateLock():
        try:
            data = load_state()
        except CapacityStateUnreadable as exc:
            # Refuse admission. Do not persist: a save here would clobber the
            # unreadable file with a false empty lease map plus the new grant.
            print(json.dumps(unreadable_admission_payload(exc), sort_keys=True))
            return 2
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
            lane_agent_cap = 0 if agent_cap <= 0 else max(1, agent_cap - BULK_POOL_RESERVE)
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
            # None is meaningful: no live controller beacon owned this launch.
            # The acquire helper's own pid is not controller identity.
            "controller_pid": args.controller_pid,
            # Claimant liveness closes the acquire-to-worker-attach race without
            # conflating this short-lived launcher with controller ownership.
            "claimant_pid": os.getpid(),
            "worker_pid": args.worker_pid,
            "mem_mb": rss_mb,
            "priority": priority,
            "state": "active",
            "process_identity_schema": PROCESS_IDENTITY_SCHEMA,
            "started_at": iso(),
            "expires_at": iso(utc_now() + ttl),
        }
        for role in ("controller", "claimant", "worker"):
            identity = _process_start_identity(lease.get(f"{role}_pid"))
            if identity is not None:
                lease[f"{role}_identity"] = identity
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


def pid_liveness(pid: int | None) -> bool | None:
    """Tri-state wrapper used by reclaim; missing pid is confirmed dead."""
    return _probe_pid_liveness(pid)


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    return goalflight_compat.pid_alive(pid)


def stale_active_leases(data: dict) -> list[dict]:
    """Active leases with no live worker, controller, or pre-attach claimant."""
    stale: list[dict] = []
    for lease in active_leases(data):
        if retained_live_scope_holds_capacity(lease):
            continue
        controller_pid = lease.get("controller_pid")
        worker_pid = lease.get("worker_pid")
        if worker_pid is not None:
            if lease.get("worker_pgid"):
                if attached_worker_group_holds_capacity(lease):
                    continue
                stale.append(lease)
                continue
            if _pid_holds_capacity(worker_pid, lease):
                continue
            stale.append(lease)
            continue
        # No worker ever attached. The question is whether anything can still
        # attach one -- which is the CLAIMANT's job, not the controller's.
        #
        # Keying on controller_pid made these leases immortal: a controller is a
        # long-running session, so it outlives every lease it requests. Measured
        # 2026-08-31 -- 29 leases with worker=none, claimant=dead,
        # controller=alive, oldest 14.2h, 20 of them held by dispatches whose
        # own state was `queued`. Queued work therefore held the capacity that
        # queued work needed in order to launch, and the pool drained until
        # nothing could start. release-stale refused them all, correctly by its
        # own logic and uselessly in practice.
        #
        # A live claimant still protects the acquire-then-spawn window, which is
        # the race this branch exists for. A claimant that cannot be probed is
        # NOT proven gone and holds, so an indeterminate read never authorises
        # reclamation.
        claimant_pid = lease.get("claimant_pid")
        if _pid_holds_capacity(claimant_pid, lease):
            continue
        if claimant_pid is None and _pid_holds_capacity(controller_pid, lease):
            # Never had a distinct claimant: fall back to the requester so a
            # controller-held lease is not reclaimed out from under itself.
            continue
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
        try:
            data = load_state()
        except CapacityStateUnreadable as exc:
            payload = {
                "error": str(exc),
                "reason": CAPACITY_STATE_UNREADABLE,
                "measured": False,
            }
            if args.json:
                print(json.dumps(payload, sort_keys=True))
            else:
                print(f"capacity: unreadable: {exc}", file=sys.stderr)
            return 1
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
        retained_part = ""
        if lease.get("reason") == INDETERMINATE_LIVE_REASON:
            retained_part = (
                f" retained-indeterminate-pgid={lease.get('accounted_live_pgid')}"
                f" recheck-after={lease.get('accounted_live_until')}"
            )
        print(f"- {lease['lease_id']} agent={lease['agent']} dispatch={lease.get('dispatch_id')} mem={lease.get('mem_mb')}MB{prio_part}{retained_part}")
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
    try:
        return args.func(args)
    except CapacityStateUnreadable as exc:
        print(json.dumps(unreadable_admission_payload(exc), sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
