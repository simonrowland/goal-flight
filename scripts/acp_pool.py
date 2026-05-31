"""Production-shaped wrapper around AcpProcessPool with crash-safe cleanup.

Two pieces:

1. `managed_pool()` — async context manager that wires SIGINT/SIGTERM/atexit
   handlers to call `pool.shutdown()` so controller crashes don't orphan
   workers (beyond what `cleanup_ghosts()` on next start can handle).
2. `compute_pool_ceiling()` — reads `docs-private/env-caveats.md` for box RAM,
   computes a `max_processes` ceiling using the worst-case worker RSS budget,
   caps at the AcpProcessPool default (20).

Intended use from goal-flight's per-chunk loop (`commands/execute.md` `[acp]` branch):

```python
async with managed_pool(agents_config, env_caveats_path=Path("docs-private/env-caveats.md")) as pool:
    for chunk in queue:
        conn = await pool.get_or_create(chunk.agent, chunk.session_id, chunk.cwd)
        result = await run_prompt(conn, dispatch_prompt(chunk))
        markers = extract_markers(result.text)
        ...
```

Signal handlers + atexit are registered on context entry, unregistered on exit
(so this doesn't leak global state if the controller wraps multiple pools).
"""

import asyncio
import atexit
import contextlib
import logging
import re
import signal
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Callable

import goalflight_compat
from goalflight_acp_client import AcpProcessPool

try:
    import goalflight_capacity
except Exception:  # pragma: no cover - keep acp_pool usable standalone
    goalflight_capacity = None

log = logging.getLogger("goal-flight.acp_pool")

# Defaults derived from empirical measurements 2026-05-18 (see env-caveats.md).
# Update if a re-measure shifts the worst-case worker.
DEFAULT_WORST_CASE_WORKER_RSS_MB = 1200  # cursor-agent peak
DEFAULT_CONTROLLER_RESERVE_MB = 2048      # controller + Claude Code + headroom
DEFAULT_HARD_CAP = 20                     # AcpProcessPool's own default
# Safe default when env-caveats is missing / malformed (don't fail open to the
# hard_cap on an unknown box — 20 cursor workers ≈ 24 GB RSS would OOM small Macs).
CONSERVATIVE_FALLBACK_CEILING = 4

_RAM_LINE_RE = re.compile(r"^-\s+RAM:\s+([\d.]+)\s+GB\s+\((\d+)\s+MB\s+total\)", re.MULTILINE)


def compute_pool_ceiling(
    env_caveats_path: Path,
    *,
    worst_case_worker_rss_mb: int = DEFAULT_WORST_CASE_WORKER_RSS_MB,
    controller_reserve_mb: int = DEFAULT_CONTROLLER_RESERVE_MB,
    hard_cap: int = DEFAULT_HARD_CAP,
) -> int:
    """Compute max concurrent ACP workers given the box's RAM.

    Prefer the procedural machine-capacity profile. Fall back to reading
    `docs-private/env-caveats.md` for the RAM_MB line written by
    `scripts/probe-box-capacity.sh`. Returns min(hard_cap, computed_ceiling).
    Floors at 1 (never returns 0 or negative — pool of 1 is still useful).

    **Fallback policy** — be conservative when we don't know the box:
    - **env-caveats missing entirely** (likely fresh install where init.md step 1.5
      hasn't run): return `CONSERVATIVE_FALLBACK_CEILING` (4). The hard_cap=20
      default would happily spawn 20 cursor workers (~24 GB RSS) on an 8 GB box.
      Log a warning instructing the user to run `scripts/probe-box-capacity.sh`.
    - **env-caveats exists but unparseable / RAM line malformed**: return
      `CONSERVATIVE_FALLBACK_CEILING` (4). The file shouldn't be malformed if it
      came from the probe script; if it did, the user edited it manually and an
      ambient-conservative fallback is safer than assuming hard_cap.
    """
    if not env_caveats_path.exists():
        log.warning(
            "env-caveats not found at %s; defaulting to conservative pool=%d. "
            "Run scripts/probe-box-capacity.sh to capture this box's actual RAM "
            "for a higher ceiling.", env_caveats_path, CONSERVATIVE_FALLBACK_CEILING,
        )
        return CONSERVATIVE_FALLBACK_CEILING
    try:
        content = env_caveats_path.read_text()
    except OSError as e:
        log.warning(
            "env-caveats read failed (%s); defaulting to conservative pool=%d",
            e, CONSERVATIVE_FALLBACK_CEILING,
        )
        return CONSERVATIVE_FALLBACK_CEILING
    m = _RAM_LINE_RE.search(content)
    if not m:
        log.warning(
            "env-caveats RAM line not found (malformed?); defaulting to conservative pool=%d",
            CONSERVATIVE_FALLBACK_CEILING,
        )
        return CONSERVATIVE_FALLBACK_CEILING
    ram_mb = int(m.group(2))
    if goalflight_capacity is not None:
        try:
            profile = goalflight_capacity.profile(
                type(
                    "Args",
                    (),
                    {
                        "ram_mb": ram_mb,
                        "reserve_mb": controller_reserve_mb,
                        "worst_worker_mb": worst_case_worker_rss_mb,
                        "hard_cap": hard_cap,
                        "max_total": None,
                    },
                )()
            )
            return int(profile["operating_cap"])
        except Exception as e:
            log.warning("goalflight_capacity profile failed (%s); falling back to raw env-caveats ceiling", e)

    headroom = ram_mb - controller_reserve_mb
    if headroom <= 0:
        log.warning("env-caveats says ram_mb=%d but controller_reserve=%d; pool of 1", ram_mb, controller_reserve_mb)
        return 1
    computed = headroom // worst_case_worker_rss_mb
    ceiling = max(1, min(hard_cap, computed))
    log.info(
        "pool ceiling computed: ram_mb=%d - reserve=%d = %d, / worst_worker=%d = %d, capped at %d -> %d",
        ram_mb, controller_reserve_mb, headroom, worst_case_worker_rss_mb, computed, hard_cap, ceiling,
    )
    return ceiling


DEFAULT_IDLE_TTL_SECONDS = 300.0      # 5 min — claude-class workers @ 614MB peak make this matter
DEFAULT_IDLE_CHECK_INTERVAL = 60.0    # check every minute


@contextlib.asynccontextmanager
async def managed_pool(
    agents_config: dict,
    *,
    env_caveats_path: Path | None = None,
    verbose: bool = False,
    install_signal_handlers: bool = True,
    auto_allow_tools: bool = True,
    permission_policy: Callable[[Any, list[Any], str | None], str] | None = None,
    permission_mode: str = "auto",
    permission_dir: Any = None,
    permission_inline_timeout_s: float | None = None,
    permission_user_timeout_s: float | None = None,
    context_mode: bool = True,
    os_sandbox: str = "off",
    idle_cleanup_ttl_seconds: float | None = DEFAULT_IDLE_TTL_SECONDS,
    idle_cleanup_interval_seconds: float = DEFAULT_IDLE_CHECK_INTERVAL,
) -> AsyncIterator[AcpProcessPool]:
    """Async context manager: AcpProcessPool with crash-safe cleanup.

    On entry:
      - Computes pool ceiling from env-caveats (if path supplied)
      - Installs SIGINT/SIGTERM/atexit handlers that call pool.shutdown()
      - Yields the pool

    On exit (normal or exception):
      - Calls pool.shutdown() to drain all live connections
      - Restores prior signal handlers
      - Unregisters atexit handler

    The signal handlers do NOT swallow signals — they schedule shutdown and
    re-raise so the controller sees the signal as intended (KeyboardInterrupt
    on SIGINT, SystemExit-equivalent on SIGTERM).

    auto_allow_tools: defaults to True here so the controller-as-auto-mode
    permission ROUTER is active: in-scope requests (in-worktree work,
    in-workspace MCP/elicitation) are auto-allowed so the worker perceives no
    delay, while boundary crossings (out-of-worktree targets, network/fetch) are
    ESCALATED to the user. False makes the controller auto-DENY every request
    (clean DeniedOutcome(cancelled) — the worker cancels the gated call and
    continues rather than wedging; older builds raised method_not_found here,
    which hung permission-gating adapters). Either way the request is answered
    promptly, so the worker never wedges on the permission channel.

    permission_policy: optional decision function
    (tool_call, options, cwd) -> "allow" | "deny" | "escalate" that overrides the
    scope-aware default, letting the controller fold in chunk SCOPE/FORBIDDEN and
    re-dispatch decisions. Escalations surface to the controller via the runner's
    permission_pending status (see GoalflightClient.request_permission).

    permission_mode: escalation TRANSPORT. "auto" (default) answers an escalated
    request with a cancel immediately and surfaces it via permission_pending
    (USER-CONFIRM -> re-dispatch). "inline" HOLDS the worker's permission open and
    authorizes it IN PLACE -- it never re-dispatches. The handler publishes a
    request file under permission_dir and the controller answers via
    goalflight_acp_permits. Two-phase, awake-time timeout (active_monotonic, so a
    laptop sleep does not consume the budget):
      - controller window (permission_inline_timeout_s, ~3 min): if the controller
        neither acks nor decides, AUTO-DECLINE -- the worker is denied that one
        tool and CONTINUES its turn (surfaced informationally in
        result.permission_auto_declined, reason "controller_timeout"). This is the
        resilience escape hatch: a rate-limited / crashed / asleep controller never
        blocks a worker that runs on its own provider.
      - user window (permission_user_timeout_s, ~10h): once the controller writes
        an ACK (write_ack -- "I'm asking the user"), the deadline extends so the
        worker waits for the human (coffee break). Expiry -> auto-decline, reason
        "user_timeout". A write_decision (allow/deny) ends either phase at once.
    Inline needs the router (auto_allow_tools=True) and a relay draining
    permission_dir.

    CONTROLLER CONTRACT for inline mode (the ~3 min controller window assumes it):
      1. POLL every turn: run permits.list_requests(permission_dir) at the top of
         each loop iteration while any inline worker is active. It is FILE IPC --
         the request is not a Claude message, so stacked user input cannot bury it
         in a queue; each queued message is its own turn, so you still poll on each.
      2. ACK FIRST, decide second: on a request you cannot auto-decide instantly,
         permits.write_ack(dir, key) immediately (a one-line file that flips the
         worker to the ~10h window), THEN present to the user / decide, THEN
         write_decision. The 3 min bar is "controller gets ONE turn", not "fully
         decides".
      3. BACKGROUND all >10s work so turns stay frequent and the poll cadence stays
         well under the controller window.
      4. If unavailable anyway (rate-limited / asleep), the worker auto-declines and
         continues; on recovery, re-dispatch just the declined action from the
         worker's permission_auto_declined.

    permission_dir: directory for inline request/decision/ack files. Defaults (in
    GoalflightClient) to $GOAL_FLIGHT_PERMISSION_DIR or a PID-scoped temp dir; pass
    an explicit path when the relay runs in a DIFFERENT process than the worker (so
    both sides agree on the location). Ignored in "auto" mode.

    permission_inline_timeout_s: controller-responsiveness window -- awake-seconds
    to wait for an ack-or-decision before auto-declining (default 180 = 3 min).

    permission_user_timeout_s: post-ack user-decision window -- awake-seconds to
    wait for the user's decision after the controller acks (default 36000 = 10h).

    os_sandbox: process-level sandbox profile for spawned workers. "off" keeps
    the host default. "read-only" and "workspace-write" wrap the worker
    subprocess in the host OS sandbox where available; this is separate from ACP
    permission escalation and applies before the agent process starts.

    idle_cleanup_ttl_seconds: connections with last_active older than this are
    reaped by a background task. Defaults to 300s (5 min). Critical for long
    runs that spawn many distinct session_ids — without this, idle workers
    accumulate. Claude-class workers peak at ~614 MB RSS so 10 stranded
    sessions = ~6 GB held. Pass None to disable (manual lifecycle).

    idle_cleanup_interval_seconds: how often the background task wakes to scan
    for idle connections. Defaults to 60s. Cost is one ps lookup per live
    connection per scan; negligible vs the RAM savings.
    """
    if env_caveats_path is not None:
        max_processes = compute_pool_ceiling(env_caveats_path)
    else:
        max_processes = DEFAULT_HARD_CAP

    pool = AcpProcessPool(
        agents_config,
        max_processes=max_processes,
        verbose=verbose,
        auto_allow_tools=auto_allow_tools,
        permission_policy=permission_policy,
        permission_mode=permission_mode,
        permission_dir=permission_dir,
        permission_inline_timeout_s=permission_inline_timeout_s,
        permission_user_timeout_s=permission_user_timeout_s,
        context_mode=context_mode,
        os_sandbox=os_sandbox,
    )
    pool.cleanup_ghosts()  # reap any orphans from a prior controller run

    loop = asyncio.get_running_loop()
    prior_handlers: dict[int, object] = {}
    atexit_registered = False

    def _shutdown_sync_for_atexit() -> None:
        # atexit runs after the loop has likely closed; do a best-effort sync cleanup.
        if not pool._connections:
            return
        for conn in list(pool._connections.values()):
            try:
                if conn.alive:
                    import signal as _sig
                    hard_signal = getattr(_sig, "SIGKILL", _sig.SIGTERM)
                    # POSIX uses the historical process-group kill. Native
                    # Windows has no killpg/pgid; kill_pid degrades to the
                    # tracked worker pid so atexit cleanup never raises
                    # AttributeError. If stale children survive on Windows,
                    # remember native dispatch is refused there; run dispatch
                    # under WSL for full process-tree semantics.
                    goalflight_compat.kill_pid(
                        conn.proc.pid,
                        hard_signal,
                        pgid=conn.verified_pgid,
                        process_group=True,
                    )
            except (ProcessLookupError, PermissionError, Exception):
                pass
        pool._connections.clear()

    async def _shutdown_async() -> None:
        try:
            await pool.shutdown()
        except Exception as e:
            log.warning("pool.shutdown() raised during signal handler: %s", e)

    def _handler_factory(prior, sig_num):
        def handler(signum, frame):
            log.warning("received signal %d; draining ACP pool before re-raising", signum)
            try:
                asyncio.get_event_loop().create_task(_shutdown_async())
            except Exception:
                # No loop / loop closed — fall back to sync kill
                _shutdown_sync_for_atexit()
            # Restore prior handler and re-raise so controller sees the signal
            signal.signal(sig_num, prior)
            signal.raise_signal(sig_num)
        return handler

    if install_signal_handlers:
        for sig_num in (signal.SIGINT, signal.SIGTERM):
            prior_handlers[sig_num] = signal.getsignal(sig_num)
            try:
                signal.signal(sig_num, _handler_factory(prior_handlers[sig_num], sig_num))
            except (ValueError, OSError):
                # signal.signal can fail in non-main-thread contexts; skip silently
                pass
        atexit.register(_shutdown_sync_for_atexit)
        atexit_registered = True

    # Background idle-cleanup loop — defends against RAM accumulation when
    # controllers spawn many distinct session_ids during long runs (especially
    # under rate-limit retry patterns where a fresh dispatch creates a new
    # heavyweight worker without reusing the old one).
    idle_task: asyncio.Task | None = None
    if idle_cleanup_ttl_seconds is not None and idle_cleanup_ttl_seconds > 0:
        async def _idle_cleanup_loop() -> None:
            try:
                while True:
                    await asyncio.sleep(idle_cleanup_interval_seconds)
                    try:
                        before = pool.stats["total"]
                        await pool.cleanup_idle(idle_cleanup_ttl_seconds)
                        after = pool.stats["total"]
                        if before != after:
                            log.info(
                                "managed_pool idle cleanup: reaped %d connection(s) idle > %.0fs",
                                before - after, idle_cleanup_ttl_seconds,
                            )
                    except Exception as e:
                        log.warning("managed_pool idle cleanup raised: %s", e)
            except asyncio.CancelledError:
                pass
        idle_task = asyncio.create_task(_idle_cleanup_loop())

    try:
        yield pool
    finally:
        if idle_task is not None and not idle_task.done():
            idle_task.cancel()
            try:
                await idle_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await pool.shutdown()
        except Exception as e:
            log.warning("pool.shutdown() in cleanup raised: %s", e)
        if install_signal_handlers:
            for sig_num, prior in prior_handlers.items():
                try:
                    signal.signal(sig_num, prior)
                except (ValueError, OSError):
                    pass
            if atexit_registered:
                try:
                    atexit.unregister(_shutdown_sync_for_atexit)
                except Exception:
                    pass
