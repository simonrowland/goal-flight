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

from acp_client import AcpProcessPool

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

    Reads `docs-private/env-caveats.md` for the RAM_MB line written by
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

    auto_allow_tools: defaults to True here because the goal-flight controller
    is the user-facing surface and decides per-chunk whether a tool call is
    acceptable BEFORE dispatching; once a chunk is dispatched, every tool the
    worker requests is in-scope by construction. The AcpProcessPool default
    (False) is appropriate for chat-bridge use where each tool call is its own
    user-interaction event. Pass auto_allow_tools=False here explicitly only
    if you want the worker to hang on session/request_permission for diagnostic
    purposes.

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
                    import os, signal as _sig
                    os.killpg(conn.proc.pid, _sig.SIGKILL)
            except (ProcessLookupError, PermissionError, Exception):
                pass
        pool._connections.clear()
        try:
            pool._pidfile.unlink(missing_ok=True)
        except Exception:
            pass

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
