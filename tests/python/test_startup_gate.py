"""Tests for the heavy-adapter startup serialization gate (goalflight_startup_gate).

flock locks from separate open() calls are independent even within one process
(Linux + BSD), so two StartupGate instances serialize in-process — which is what
lets us test the real behavior without spawning workers.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from goalflight_startup_gate import StartupGate  # noqa: E402


def test_non_serialized_agent_is_noop() -> None:
    async def go() -> None:
        g = StartupGate("codex-acp")
        assert g.enabled is False
        async with g:
            assert g._fh is None  # no lock taken for fast-startup adapters

    asyncio.run(go())


def test_serialized_agent_acquires_and_releases() -> None:
    async def go() -> None:
        g = StartupGate("claude")
        assert g.enabled is True
        async with g:
            assert g._fh is not None  # holds the startup flock
        assert g._fh is None  # released on exit

    asyncio.run(go())


def test_second_startup_waits_for_first() -> None:
    """The core behavior: while one Claude startup holds the gate, the next one
    blocks until it releases — startups are serialized, not concurrent."""
    async def go() -> None:
        g1 = StartupGate("claude")
        await g1.__aenter__()  # g1 holds the startup slot
        try:
            g2 = StartupGate("claude", max_wait=5.0, poll=0.05)

            async def release_g1_soon() -> None:
                await asyncio.sleep(0.4)
                g1.release()

            asyncio.ensure_future(release_g1_soon())
            loop = asyncio.get_event_loop()
            t0 = loop.time()
            await g2.__aenter__()
            waited = loop.time() - t0
            try:
                assert g2._fh is not None, "g2 never acquired the slot"
                assert waited >= 0.3, f"g2 did not wait for g1 (waited {waited:.2f}s)"
            finally:
                g2.release()
        finally:
            g1.release()

    asyncio.run(go())


def test_release_on_exception_in_body() -> None:
    """The gate must release even if the body raises (the real call site wraps
    spawn_and_handshake_with_retry, which can throw) — else a failed handshake
    would hold the slot and block every later startup."""
    async def go() -> None:
        g1 = StartupGate("claude")
        try:
            async with g1:
                assert g1._fh is not None
                raise RuntimeError("handshake boom")
        except RuntimeError:
            pass
        assert g1._fh is None, "lock not released after an exception in the body"
        g2 = StartupGate("claude", max_wait=2.0, poll=0.05)
        async with g2:
            assert g2._fh is not None, "next startup could not acquire after exception release"

    asyncio.run(go())


def test_fail_open_after_max_wait() -> None:
    """When a holder is stuck, a waiter proceeds WITHOUT the lock after max_wait
    (deadlock-free) and does NOT keep a stray fd open."""
    async def go() -> None:
        holder = StartupGate("claude")
        await holder.__aenter__()  # holds the slot for the whole test
        try:
            waiter = StartupGate("claude", max_wait=0.3, poll=0.05)
            async with waiter:
                assert waiter._fh is None, "fail-open should hold no lock + no open fd"
            assert waiter.waited >= 0.25, f"did not wait ~max_wait before failing open ({waiter.waited:.2f}s)"
        finally:
            holder.release()

    asyncio.run(go())


def test_env_override_serialize_set() -> None:
    import os
    prev = os.environ.get("GOALFLIGHT_SERIALIZE_STARTUP")
    os.environ["GOALFLIGHT_SERIALIZE_STARTUP"] = "grok"
    try:
        assert StartupGate("grok").enabled is True
        assert StartupGate("claude").enabled is False  # no longer in the set
    finally:
        if prev is None:
            os.environ.pop("GOALFLIGHT_SERIALIZE_STARTUP", None)
        else:
            os.environ["GOALFLIGHT_SERIALIZE_STARTUP"] = prev


def main() -> None:
    test_non_serialized_agent_is_noop()
    test_serialized_agent_acquires_and_releases()
    test_second_startup_waits_for_first()
    test_release_on_exception_in_body()
    test_fail_open_after_max_wait()
    test_env_override_serialize_set()
    print("OK: startup gate tests pass")


if __name__ == "__main__":
    main()
