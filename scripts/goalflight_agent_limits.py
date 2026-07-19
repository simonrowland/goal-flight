#!/usr/bin/env python3
"""Shared agent capacity defaults for goal-flight.

Pure leaf module: imports neither goalflight_capacity nor goalflight_rate_pressure.

The values below are the *committed generic baseline*. They are deliberately
conservative-by-scaling: the machine-global operating cap in goalflight_capacity
is RAM-tiered, so on a small box these high per-agent caps are never reached.
A single operator's aggressive tuning for a specific big box must NOT be baked
into this tracked file (that would export one machine's settings to every user).
Per-machine tuning lives in a gitignored local conf loaded at import time -- see
``load_local_overrides`` at the bottom of this module.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

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
    "kimi": 386,
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
    "kimi": 6,
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


# --------------------------------------------------------------------------- #
# Machine-local capacity overrides (per-operator, gitignored, NOT committed).  #
#                                                                              #
# Concurrency headroom is a property of the *machine*, not of the repo: an     #
# always-on 128GB Studio and a 16GB laptop want very different caps. Baking    #
# one operator's numbers into the tracked defaults above would ship those      #
# settings to every user of the skill. Instead, per-machine tuning lives in a  #
# small JSON file loaded here at import time and merged over the committed      #
# baseline. Absent file -> baseline stands (the common case for a fresh user). #
#                                                                              #
# Resolution order for the conf path:                                          #
#   1. $GOALFLIGHT_CAPACITY_CONF (explicit path; also how tests isolate)       #
#   2. ~/.goal-flight/capacity.local.json (durable, machine-global, outside    #
#      any repo -> inherently git-invisible; the state dir under $TMPDIR is     #
#      wiped on reboot and is the wrong home for durable tuning)               #
#                                                                              #
# Recognized keys (all optional):                                              #
#   "agent_caps":   {agent: int}  merged over DEFAULT_AGENT_CAPS               #
#   "agent_rss_mb": {agent: int}  merged over AGENT_RSS_MB                      #
#   "hard_cap":     int           raw ceiling for goalflight_capacity          #
#   "operating_total"|"max_total": int  persistent machine operating cap       #
#      (equivalent to $GOALFLIGHT_CAPACITY_MAX_TOTAL but durable; the explicit  #
#      env var and CLI --max-total still win)                                   #
# --------------------------------------------------------------------------- #


def _local_conf_path() -> Path:
    raw = os.environ.get("GOALFLIGHT_CAPACITY_CONF", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".goal-flight" / "capacity.local.json"


def load_local_overrides(path: Path | None = None) -> dict:
    """Return machine-local capacity overrides, or {} if absent/malformed.

    Never raises: a missing or unparseable conf must degrade to the committed
    baseline, never break dispatch.
    """
    conf_path = path if path is not None else _local_conf_path()
    try:
        raw = conf_path.read_text()
    except (OSError, ValueError):
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _merge_int_map(target: dict, override: object) -> None:
    if not isinstance(override, dict):
        return
    for key, value in override.items():
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            target[normalize_agent(str(key))] = parsed


LOCAL_OVERRIDES = load_local_overrides()
_merge_int_map(DEFAULT_AGENT_CAPS, LOCAL_OVERRIDES.get("agent_caps"))
_merge_int_map(AGENT_RSS_MB, LOCAL_OVERRIDES.get("agent_rss_mb"))


def _positive_int_or(value: object, default):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def local_hard_cap(default: int) -> int:
    """Conf ``hard_cap`` if set to a positive int, else ``default``."""
    return _positive_int_or(LOCAL_OVERRIDES.get("hard_cap"), default)


def local_operating_total() -> int | None:
    """Conf ``operating_total`` (or ``max_total``) as a positive int, else None."""
    value = LOCAL_OVERRIDES.get("operating_total", LOCAL_OVERRIDES.get("max_total"))
    return _positive_int_or(value, None)
