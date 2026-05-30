"""Serialize the heavy STARTUP window of fragile ACP adapters.

Some ACP adapters are cheap to run but expensive to START. `claude-code-cli-acp`
PTY-drives the interactive Claude Code TUI (hooks, LSP, plugins, keychain,
auto-memory, MCP, CLAUDE.md discovery); launching several at once starves them so
badly that even a trivial turn blows past the adapter's hardcoded 120s ceiling
(observed 2026-05-20: 4 simultaneous = 3/4 complete; the SAME 4 staggered = 4/4).

Rather than a machine-specific fixed stagger interval (a constant baselined on one
laptop is wrong on a slower box or under load), serialize the STARTUP itself: hold
a per-agent advisory lock from just before spawn until the handshake completes,
then release it so the TURN can overlap with other workers. The lock is held for
exactly as long as startup actually takes ON THIS MACHINE — no hardcoded timing.
`flock` is released automatically by the OS if a runner dies mid-startup, so there
is no stale-lock cleanup to get wrong.

Only the spawn→handshake window is serialized; turns then run concurrently up to
the capacity cap. This is what lets the count cap stay high while startups never
contend. codex-acp and grok are fast-startup AND fast-turn (clean at high
concurrency) so they are NOT gated. cursor is also fast to START, so the
StartupGate does not help it — but cursor's CLOUD backend is SLOW per turn
(~0% CPU blocked on the network), which is handled by the first-token wedge grace
plus a lower capacity cap (3), not by startup serialization.

Tunable via env `GOALFLIGHT_SERIALIZE_STARTUP` (comma-separated agent names);
default serializes the Claude TUI adapter only.
"""

from __future__ import annotations

import asyncio
import os

import goalflight_compat
import goalflight_compat as fcntl

_LOCK_DIR = goalflight_compat.temp_base() / "goal-flight-startup-locks"


def _serialize_set() -> set[str]:
    raw = os.environ.get("GOALFLIGHT_SERIALIZE_STARTUP")
    if raw is None:
        return {"claude", "claude-code-cli-acp"}
    return {a.strip() for a in raw.split(",") if a.strip()}


class StartupGate:
    """Async context manager that serializes startup for heavy ACP adapters.

    Acquire just before spawning the worker; the context exits (releases) once the
    handshake has completed, so the worker's TURN overlaps freely with others.
    No-op for agents not in the serialize set. Never blocks a dispatch forever:
    after `max_wait` it proceeds without the lock (fail-open) so a stuck holder
    can't deadlock the queue.
    """

    def __init__(self, agent: str, *, max_wait: float = 600.0, poll: float = 0.5):
        # max_wait must exceed the worst-case hold: spawn_and_handshake_with_retry
        # does 2 attempts × (initialize 60s + session_new 60s) ≈ 240s, so a waiter
        # has to tolerate that before failing open. 600s leaves margin for a couple
        # of queued slow handshakes; normal handshakes are ~10-15s (codex review P2).
        self.agent = agent
        self.max_wait = max_wait
        self.poll = poll
        self._fh = None
        self.waited = 0.0  # observability: how long we waited for the slot

    @property
    def enabled(self) -> bool:
        return self.agent in _serialize_set()

    async def __aenter__(self) -> "StartupGate":
        if not self.enabled:
            return self
        try:
            _LOCK_DIR.mkdir(parents=True, exist_ok=True)
            self._fh = open(_LOCK_DIR / f"{self.agent.replace('/', '_')}.lock", "w")
        except OSError:
            self._fh = None  # can't create the lock → fail open, don't block dispatch
            return self
        loop = asyncio.get_running_loop()
        start = loop.time()
        deadline = start + self.max_wait
        while True:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.waited = loop.time() - start
                return self  # got the startup slot
            except (BlockingIOError, OSError):
                if loop.time() > deadline:
                    # Fail-open: after max_wait, proceed WITHOUT the slot rather
                    # than deadlock the dispatch — a stuck holder must never block
                    # the queue forever. Trades a rare 2-concurrent-startup herd
                    # for guaranteed liveness. Close the fd: we hold no lock, so
                    # don't keep it open through the (now ungated) startup.
                    self.waited = loop.time() - start
                    try:
                        self._fh.close()
                    except OSError:
                        pass
                    self._fh = None
                    return self
                await asyncio.sleep(self.poll)

    async def __aexit__(self, *exc) -> None:
        self.release()

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
