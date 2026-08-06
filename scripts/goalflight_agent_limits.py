#!/usr/bin/env python3
"""Shared agent capacity defaults for goal-flight.

Pure leaf module: imports neither goalflight_capacity nor goalflight_rate_pressure.

Also hosts the agent-handle plumbing: ``normalize_agent``, the retired-handle
legacy mapping (``LEGACY_AGENT_HANDLES`` / ``canonical_agent_label`` /
``moonshot_family``) consulted by RECORD-READING paths, and ``cap_pool``.
Input validation (dispatch presets, capacity acquire) must not canonicalize.

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
    "moonshot": 386,
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
    "moonshot": 6,
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


# Retired dispatch handles -> successor handle. Ledgers, leases, and status
# files written before a rename carry the old label forever, so RECORD-READING
# paths (rendering, reconciliation, marker quirks, capacity accounting, usage
# evidence) map the old label onto its successor here -- old records mean the
# successor family. INPUT paths (dispatch preset validation, capacity acquire)
# must NOT consult this map: new input under the retired handle fails with the
# normal unknown-agent error, which is the migration mechanism.
LEGACY_AGENT_HANDLES = {"kimi": "moonshot"}


def canonical_agent_label(agent: object) -> str:
    """Record-reading boundary: normalize and map retired handles to successors."""
    label = normalize_agent(str(agent or ""))
    return LEGACY_AGENT_HANDLES.get(label, label)


def moonshot_family(agent: object) -> bool:
    """True when a label -- new input or legacy record value -- names the
    Moonshot (kimi CLI) worker family. Drives the kimi-output marker dialect
    for both new ``moonshot`` dispatches and legacy ``kimi`` records."""
    return canonical_agent_label(agent) == "moonshot"


def cap_pool(agent: str) -> str:
    """Map agent label to the shared capacity pool key."""
    agent = canonical_agent_label(agent)
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
            # Canonicalize so a pre-rename machine profile keyed by a retired
            # handle (e.g. "kimi") keeps tuning the same lane ("moonshot").
            target[canonical_agent_label(str(key))] = parsed


LOCAL_OVERRIDES = load_local_overrides()
# Snapshot the committed baseline BEFORE local overrides are merged in place:
# seeding a fresh machine must plant the shipped defaults, not whatever this
# particular box happens to have been hand-tuned to.
COMMITTED_AGENT_CAPS = dict(DEFAULT_AGENT_CAPS)
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


# --- capacity profile seeding -------------------------------------------------
# A fresh machine had no capacity profile at all: nothing in the installer, init,
# or doctor created ~/.goal-flight/capacity.local.json, so every new install ran
# on the committed generic baseline while the tuned values lived only in one
# operator's hand-written file. Seeding gives every install a profile with real
# provenance.

CAPACITY_RESERVE_MB = 3072          # leave this much for the OS and the controller
CAPACITY_MB_PER_AGENT = 400         # fleet-average worker RSS; the acquire-time
                                    # RSS budget and worst_worker_mb still clamp
                                    # real concurrency, so this sizes the CEILING
                                    # input, not the achievable parallelism.


def _system_memory_mb() -> int | None:
    """Physical RAM in MB, or None when it cannot be determined."""
    try:
        import subprocess

        out = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5
        )
        if out.returncode == 0 and out.stdout.strip().isdigit():
            return int(out.stdout.strip()) // (1024 * 1024)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return (pages * page_size) // (1024 * 1024)
    except (ValueError, OSError, AttributeError):
        pass
    return None


def recommended_hard_cap(default: int = 40) -> int:
    """Machine ceiling from RAM: (total - reserve) / per-agent.

    Returns ``default`` when memory cannot be read, so an unknown machine gets
    the conservative committed baseline rather than a guess.
    """
    total_mb = _system_memory_mb()
    if not total_mb or total_mb <= CAPACITY_RESERVE_MB:
        return default
    return max(1, (total_mb - CAPACITY_RESERVE_MB) // CAPACITY_MB_PER_AGENT)


def seed_capacity_conf(path: Path | None = None, *, force: bool = False) -> dict:
    """Write a starter capacity profile when none exists.

    Never overwrites an existing profile without ``force``: the file is the
    operator's tuning record, and clobbering it would discard measurements the
    committed defaults cannot reproduce.
    """
    target = Path(path) if path is not None else _local_conf_path()
    if target.exists() and not force:
        return {"status": "exists", "path": str(target)}
    cap = recommended_hard_cap()
    profile = {
        "_comment": (
            "Machine-local goal-flight capacity tuning. Gitignored by location. "
            "hard_cap seeded from (system memory - "
            f"{CAPACITY_RESERVE_MB}MB reserve) / {CAPACITY_MB_PER_AGENT}MB per agent; "
            "the acquire-time RSS budget and worst_worker_mb still bound real "
            "concurrency. agent_caps are measured provider tolerances. Record a "
            "reason and a date when you change a value."
        ),
        "hard_cap": cap,
        "operating_total": cap,
        "agent_caps": dict(COMMITTED_AGENT_CAPS),
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:
        return {"status": "error", "path": str(target), "error": str(exc)}
    return {"status": "created", "path": str(target), "hard_cap": cap}
