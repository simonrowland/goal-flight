#!/usr/bin/env python3
"""Priority-lane contract for goalflight_capacity.cmd_acquire.

Acquire is single-shot try-or-block (no queue), so under multi-controller
bursts bulk retries statistically crowd out critical fix dispatches. Lanes
reserve headroom instead of queueing:
  bulk     -> may not take the last BULK_GLOBAL_RESERVE machine slots nor the
              last BULK_POOL_RESERVE pool slot
  normal   -> exactly the legacy behavior (and the default for legacy callers
              whose Namespace has no priority attribute at all)
  critical -> borrows CRITICAL_*_BORROW beyond operating/pool caps, never past
              the RAM raw ceiling; pool borrow yields to adaptive rate-pressure
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile

import support  # noqa: F401  (sys.path setup)

os.environ["GOALFLIGHT_STATE_DIR"] = tempfile.mkdtemp(prefix="gf-lane-test-")

import goalflight_capacity as cap  # noqa: E402


def _ns(agent: str, **kw) -> argparse.Namespace:
    base = dict(
        agent=agent, dispatch_id=f"t-{kw.get('lease_id') or agent}", prompt_id=None,
        project_root="/tmp/x", worker_cwd="/tmp/x", worktree_path=None,
        controller_pid=1, worker_pid=None, lease_id=None, mem_mb=10,
        agent_cap=None, priority="normal", ttl_s=600, ram_mb=131072,
        reserve_mb=cap.DEFAULT_RESERVE_MB, worst_worker_mb=cap.DEFAULT_WORST_WORKER_MB,
        hard_cap=20, max_total=6, rate_pressure_window_s=1, rate_pressure_threshold=99,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def acquire(agent: str, **kw) -> tuple[int, dict]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cap.cmd_acquire(_ns(agent, **kw))
    return rc, json.loads(out.getvalue())


def case_bulk_reserves_global_headroom() -> None:
    for i in range(3):
        rc, p = acquire("codex", lease_id=f"L{i}")
        assert rc == 0, p
    rc, p = acquire("codex", priority="bulk")
    assert rc == 2 and p["reason"] == "machine_worker_cap" and p["lane_max_total"] == 3, p
    rc, p = acquire("codex", lease_id="L3")
    assert rc == 0, p


def case_critical_borrows_past_operating_cap() -> None:
    for i in range(4, 6):
        rc, p = acquire("codex", lease_id=f"L{i}")
        assert rc == 0, p
    rc, p = acquire("codex")
    assert rc == 2 and p["reason"] == "machine_worker_cap", p
    rc, p = acquire("codex", priority="critical", lease_id="LC")
    assert rc == 0 and p["lease"]["priority"] == "critical", p


def case_pool_lanes() -> None:
    rc, p = acquire("grok-code", agent_cap=2, max_total=18, lease_id="G0")
    assert rc == 0, p
    rc, p = acquire("grok-research", agent_cap=2, max_total=18, priority="bulk")
    assert rc == 2 and p["reason"] == "agent_worker_cap" and p["lane_agent_cap"] == 1, p
    rc, p = acquire("grok-acp", agent_cap=2, max_total=18, lease_id="G1")
    assert rc == 0, p
    rc, p = acquire("grok-code", agent_cap=2, max_total=18)
    assert rc == 2 and p["reason"] == "agent_worker_cap", p
    rc, p = acquire("grok-code", agent_cap=2, max_total=18, priority="critical", lease_id="GC")
    assert rc == 0, p


def case_legacy_namespace_without_priority_attr() -> None:
    ns = _ns("codex", max_total=18, lease_id="LOLD")
    del ns.priority
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cap.cmd_acquire(ns)
    p = json.loads(out.getvalue())
    assert rc == 0 and p["lease"]["priority"] == "normal", p


def case_unknown_priority_rejected() -> None:
    rc, p = acquire("codex", max_total=18, priority="urgent")
    assert rc == 2 and p["decision"] == "error", p


def case_critical_pool_borrow_yields_to_pressure() -> None:
    """Under adaptive rate-pressure, critical must NOT borrow past the reduced
    pool cap — provider pushback wins over priority."""
    orig = cap.adaptive_agent_cap
    cap.adaptive_agent_cap = lambda agent, base, pressure: (1, {"synthetic": True})
    try:
        rc, p = acquire("claude", agent_cap=2, max_total=40, lease_id="CL0")
        assert rc == 0, p
        rc, p = acquire("claude", agent_cap=2, max_total=40, priority="critical")
        assert rc == 2 and p["reason"] == "adaptive_rate_pressure", p
        assert p["lane_agent_cap"] == 1, p  # no +2 borrow under pressure
    finally:
        cap.adaptive_agent_cap = orig


def case_rss_budget_binds_critical() -> None:
    rc, p = acquire("codex", max_total=40, priority="critical", mem_mb=10**9)
    assert rc == 2 and p["reason"] == "rss_budget" and p["priority"] == "critical", p


def case_critical_global_borrow_clamped_to_raw_ceiling() -> None:
    """max_total=7, hard_cap=8 -> raw ceiling 8; critical lane = min(7+2, 8) = 8."""
    rc, p = acquire("codex", max_total=7, hard_cap=8, priority="critical")
    assert rc == 2 and p["reason"] == "machine_worker_cap", p
    assert p["lane_max_total"] == 8, p


def case_bulk_floor_at_tiny_max_total() -> None:
    rc, p = acquire("codex", max_total=2, priority="bulk")
    assert rc == 2 and p["lane_max_total"] == 1, p


def case_bulk_pool_floor_at_agent_cap_one() -> None:
    rc, p = acquire("grok-code", agent_cap=1, max_total=40, priority="bulk")
    assert rc == 2 and p["reason"] == "agent_worker_cap" and p["lane_agent_cap"] == 1, p


def case_cooldown_blocks_critical() -> None:
    with cap.StateLock():
        data = cap.load_state()
        data.setdefault("cooldowns", {})["opencode"] = {
            "agent": "opencode",
            "reason": "test-cooldown",
            "until": cap.iso(cap.utc_now() + __import__("datetime").timedelta(seconds=120)),
        }
        cap.save_state(data)
    rc, p = acquire("opencode", max_total=40, priority="critical")
    assert rc == 2 and str(p["reason"]).startswith("cooldown:"), p


def case_status_renders_priority() -> None:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cap.cmd_status(_ns("codex", json=False))
    text = out.getvalue()
    assert "prio=critical" in text, text[:400]


def main() -> None:
    case_bulk_reserves_global_headroom()
    case_critical_borrows_past_operating_cap()
    case_pool_lanes()
    case_legacy_namespace_without_priority_attr()
    case_unknown_priority_rejected()
    case_critical_pool_borrow_yields_to_pressure()
    case_rss_budget_binds_critical()
    case_critical_global_borrow_clamped_to_raw_ceiling()
    case_bulk_floor_at_tiny_max_total()
    case_bulk_pool_floor_at_agent_cap_one()
    case_cooldown_blocks_critical()
    case_status_renders_priority()
    print("OK: capacity priority-lane tests pass")


if __name__ == "__main__":
    main()
