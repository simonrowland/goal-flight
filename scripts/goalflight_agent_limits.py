#!/usr/bin/env python3
"""Shared agent capacity defaults for goal-flight.

Pure leaf module: imports neither goalflight_capacity nor goalflight_rate_pressure.
"""

from __future__ import annotations

# grok RSS re-measured live 2026-07-01 (128GB M5, operator flag): a running grok
# worker is ~144MB self-RSS and ~200-390MB counting its node/MCP child tree, vs
# the original 111MB. Set to 200 (tree-inclusive, matching codex's tree-ish 386).
# Immaterial to the RAM ceiling on this box (129GB budget) but keeps the RSS
# budget honest; the binding grok constraint is the provider cap, not RAM.
AGENT_RSS_MB = {
    "grok": 200,
    "grok-acp": 200,
    "grok-code": 200,
    "grok-research": 200,
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
# Sized to support multi-session parallel work. Adaptive busy-signal walkback
# reduces effective caps at acquire time when recent dispatch-ledger failures
# show provider pressure. Static caps remain starting defaults; adaptive caps
# are transient and never mutate this map or capacity.json.
DEFAULT_AGENT_CAPS = {
    # cursor-agent talks to Cursor's cloud backend, which is slow: a trivial prompt
    # takes roughly 34s solo and 57s at 3-concurrent, with the process blocked on
    # the network. It runs concurrently reliably up to about 3; at 5, mid-stream
    # gaps exceed the heartbeat wedge window. cursor and cursor-agent share one
    # Cursor subscription budget.
    "cursor": 3,
    "cursor-agent": 3,
    "opencode": 10,
    "opencode-acp": 10,
    "opencode-bash-tail": 10,
    # claude-code-cli-acp PTY-drives the interactive Claude TUI and tails the
    # session transcript with a 120s per-turn timeout. The startup gate serializes
    # the spawn/handshake window, so concurrent turns are safe and the count cap
    # can stay at 5.
    "claude": 5,
    "claude-code-cli-acp": 5,
    # codex and grok caps are intentionally high; adaptive walkback halves
    # effective caps on real provider rejections.
    # grok pool cap raised 20->30 (2026-07-01, operator-requested): heavy grok
    # prompt volume on a 128GB/18-core M5 with zero observed provider pressure
    # (1118-record ledger sweep clean) and grok worker RSS only ~200MB, so RAM is
    # not the bound. NOTE: grok(30)+codex(18)=48 > the shared global operating
    # cap (32), so grok reaches 30 only when codex is light; the global cap still
    # arbitrates joint load. Same "workers are network-bound, not CPU-bound"
    # reasoning as the 2026-06-16 global 20->32 bump.
    "codex": 18,
    "codex-acp": 18,
    "grok": 30,
    "grok-acp": 30,
    "grok-code": 30,
    "grok-research": 30,
    # Gateway orchestrators: lower cap, longer orchestration latency.
    "herm-worker": 2,
    "cla-worker": 2,
    "paperclip": 2,
}

# Bash-tail and dispatch presets that share one engine/provider concurrency budget.
AGENT_CAP_POOL: dict[str, str] = {
    "grok-code": "grok",
    "grok-research": "grok",
    "grok-acp": "grok",
    "grok-bash-tail": "grok",
}


def normalize_agent(agent: str) -> str:
    return agent.strip().lower()


def cap_pool(agent: str) -> str:
    """Map agent label to the shared capacity pool key."""
    agent = normalize_agent(agent)
    return AGENT_CAP_POOL.get(agent, agent)
