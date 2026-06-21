#!/usr/bin/env python3
"""Shared agent capacity defaults for goal-flight.

Pure leaf module: imports neither goalflight_capacity nor goalflight_rate_pressure.
"""

from __future__ import annotations

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
    "codex": 18,
    "codex-acp": 18,
    "grok": 20,
    "grok-acp": 20,
    "grok-code": 20,
    "grok-research": 20,
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
