#!/usr/bin/env python3
"""Adaptive rate-limit pressure detector for goal-flight.

Reads the dispatch ledger and recent worker status files, classifies failures
into rate-limit pressure per provider, and emits a JSON recommendation the
orchestrator reads before its next dispatch decision. Read-only in v1 — the
orchestrator decides whether to act; this script never mutates capacity state.

Provider model
--------------
Per-label caps in `goalflight_capacity.DEFAULT_AGENT_CAPS` are PROCESS-COUNT
caps (RAM-aware). Rate limits, on the other hand, are vendor/provider-level.
This script groups workers by the provider whose budget they consume:

  anthropic-session    claude (Agent-tool subagent — shares orchestrator budget)
  anthropic-cli-acp    claude-code-cli-acp (separate Claude Code session)
  anthropic-api        claude (claude -p headless — API billing)
  openai               codex, codex-acp (same OpenAI subscription / API)
  xai                  grok
  cursor               cursor, cursor-agent (same Cursor subscription)

Two codex labels share OpenAI budget; two cursor labels share Cursor budget.
The walkback enforces ONE recommendation per provider regardless of how many
labels point at it.

Detection
---------
Rate-limit signatures vary by vendor. We scan record state + status-file
error fields for these patterns (case-insensitive substring match):

  - "rate_limit", "rate-limit", "rate limit"
  - "429"  (HTTP)
  - "you've hit your limit", "usage limit"
  - "anthropic.RateLimitError", "openai.RateLimitError"
  - "session_limit"
  - "blocked_session_limit"  (goal-flight's own classification)

Pressure rule
-------------
DEFAULT: 3+ rate-limit signatures for the same provider within the last 600s
(10 minutes) = under pressure. Tune via env / flags.

Recommendation
--------------
For each provider under pressure:
  - reduce that provider's effective cap by 50% (floor 1)
  - re-route task categories the SKILL.md routing table defaults onto that
    provider toward the documented fallback

The orchestrator reads this JSON, surfaces a STATUS marker to the user, and
optionally re-routes its next dispatch. Mutation of capacity state is
explicitly out of scope for v1 — keep the policy human-supervisable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import goalflight_compat

SCHEMA = "goalflight.rate-pressure.v1"

# Agent label → provider key. New workers extend this map; the walkback
# auto-handles them as long as the provider classification is correct.
#
# Bash-tail labels (emitted by scripts/watch-dispatch-tail.sh and recorded in
# the ledger by the legacy bash-tail dispatch path) map to the same providers
# as their ACP / Agent equivalents — same vendor budget, different dispatch
# shape. `claude-bash-tail` specifically goes to anthropic-api (NOT
# anthropic-session) because the bash-tail path uses `claude -p` which is
# API-billed, separate from the orchestrator's session budget.
AGENT_TO_PROVIDER: dict[str, str] = {
    "claude": "anthropic-session",
    "claude-bash-tail": "anthropic-api",
    "claude-code-cli-acp": "anthropic-cli-acp",
    "codex": "openai",
    "codex-acp": "openai",
    "codex-bash-tail": "openai",
    "grok": "xai",
    "grok-bash-tail": "xai",
    "cursor": "cursor",
    "cursor-agent": "cursor",
    "opencode": "openai",
    "opencode-acp": "openai",
    "opencode-bash-tail": "openai",
}

# Default task-category fallback when a provider is under pressure. The
# orchestrator can override per-chunk; this is a sensible default that mirrors
# the routing table in SKILL.md.
PROVIDER_FALLBACK: dict[str, list[str]] = {
    # When the orchestrator's own Claude budget is under pressure, push
    # everything we can to codex/grok/cursor.
    "anthropic-session": ["codex", "cursor", "grok"],
    # claude-code-cli-acp wraps a separate session — same vendor failover
    # logic applies if THAT session's budget is hit.
    "anthropic-cli-acp": ["codex", "cursor", "grok"],
    "anthropic-api":     ["codex", "cursor", "grok"],
    "openai":            ["cursor", "grok"],
    "xai":               ["codex", "cursor"],
    "cursor":            ["codex", "grok"],
}

# Substring patterns indicating a rate-limit failure. Case-insensitive.
RATE_LIMIT_PATTERNS: tuple[str, ...] = (
    "rate_limit",
    "rate-limit",
    "rate limit",
    "429",
    "you've hit your limit",
    "usage limit",
    "anthropic.ratelimiterror",
    "openai.ratelimiterror",
    "session_limit",
    "blocked_session_limit",
)


def provider_for(agent_label: str) -> str | None:
    """Map an agent label to its provider key. Returns None for unknown labels."""
    return AGENT_TO_PROVIDER.get(agent_label)


def default_fleet_dir() -> Path:
    return Path(os.environ.get("GOALFLIGHT_FLEET_DIR", Path.home() / ".goal-flight" / "fleet")).expanduser()


def load_billing_accounts(fleet_dir: Path | None = None) -> dict | None:
    fleet_dir = fleet_dir or default_fleet_dir()
    path = fleet_dir / "billing-accounts.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def agent_limit_pool_map(billing_doc: dict | None) -> dict[str, str]:
    """Map agent label → limit_pool_id from fleet billing facts."""
    out: dict[str, str] = {}
    if not billing_doc:
        return out
    for account in billing_doc.get("accounts") or []:
        pool_id = account.get("limit_pool_id")
        if not pool_id:
            continue
        for label in account.get("agent_labels") or []:
            out[str(label)] = str(pool_id)
    return out


def budget_key_for_agent(agent_label: str, *, pool_map: dict[str, str] | None = None) -> str | None:
    """Prefer limit_pool_id; fall back to legacy provider key."""
    if pool_map:
        pool = pool_map.get(agent_label)
        if pool:
            return f"pool:{pool}"
    provider = provider_for(agent_label)
    if provider:
        return f"provider:{provider}"
    return None


def _default_state_dir() -> Path:
    return Path(os.environ.get("GOALFLIGHT_STATE_DIR", goalflight_compat.default_state_dir()))


def _read_record(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _read_status(record: dict) -> dict | None:
    """Read the worker's status JSON if one is referenced and exists."""
    status_path = record.get("status_path")
    if not status_path:
        return None
    p = Path(status_path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def detect_rate_limit_signature(record: dict, status: dict | None) -> bool:
    """Return True if this dispatch shows rate-limit failure signs.

    Checks: record.state (goal-flight's classification), status.error fields,
    and status.text_excerpt for vendor-specific patterns.

    NOTE: `blocked_auth` is deliberately NOT counted as rate-limit pressure.
    Auth failures are provider-availability problems (missing/invalid
    credentials) that need credential repair, not cap-halving. Counting them
    here would trigger walkback recommendations that mask the real fix.
    """
    state = (record.get("state") or "").lower()
    if state == "blocked_session_limit":
        return True
    if state == "failed":
        # Need to look at the status payload for the actual error shape.
        pass
    elif state not in {"failed", "inconclusive_timeout"}:
        # Successful, pending, or auth-blocked dispatches don't count.
        return False

    if not status:
        return False
    haystack_parts: list[str] = []
    err = status.get("error")
    if err:
        if isinstance(err, dict):
            haystack_parts.append(json.dumps(err))
        else:
            haystack_parts.append(str(err))
    excerpt = status.get("text_excerpt")
    if excerpt:
        haystack_parts.append(str(excerpt))
    haystack = " ".join(haystack_parts).lower()
    if not haystack:
        return False
    return any(pat in haystack for pat in RATE_LIMIT_PATTERNS)


def pressure_per_provider(
    records: list[dict],
    window_seconds: int = 600,
    now_ts: float | None = None,
    *,
    pool_map: dict[str, str] | None = None,
) -> dict[str, int]:
    """Count rate-limit signatures per budget key within window.

    Keys are `pool:<limit_pool_id>` when fleet billing map is available,
    otherwise `provider:<provider_key>`.
    """
    if now_ts is None:
        now_ts = time.time()
    cutoff_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now_ts - window_seconds))
    counts: dict[str, int] = {}
    for record in records:
        agent = record.get("agent")
        key = budget_key_for_agent(agent, pool_map=pool_map) if agent else None
        if not key:
            continue
        updated = record.get("updated_at") or record.get("started_at") or ""
        if not updated or updated < cutoff_iso:
            continue
        status = _read_status(record)
        if not detect_rate_limit_signature(record, status):
            continue
        counts[key] = counts.get(key, 0) + 1
    return counts


def recommend(
    pressure: dict[str, int],
    current_caps: dict[str, int],
    threshold: int = 3,
    *,
    pool_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a recommendation payload.

    `pressure`: {budget_key: count} from pressure_per_provider().
    """
    label_groups: dict[str, list[str]] = {}
    for agent_label in AGENT_TO_PROVIDER:
        key = budget_key_for_agent(agent_label, pool_map=pool_map)
        if key:
            label_groups.setdefault(key, []).append(agent_label)

    out: dict[str, Any] = {
        "schema": SCHEMA,
        "threshold": threshold,
        "providers_under_pressure": [],
        "providers_observed": list(pressure.keys()),
        "budget_keys_observed": list(pressure.keys()),
    }
    for budget_key, count in sorted(pressure.items(), key=lambda kv: -kv[1]):
        if count < threshold:
            continue
        labels = label_groups.get(budget_key, [])
        recommended_caps = {
            label: max(1, current_caps.get(label, 5) // 2)
            for label in labels
        }
        provider = budget_key.split(":", 1)[1] if budget_key.startswith("provider:") else None
        limit_pool_id = budget_key.split(":", 1)[1] if budget_key.startswith("pool:") else None
        if limit_pool_id and pool_map:
            for label, pool in pool_map.items():
                if pool == limit_pool_id and label not in labels:
                    labels.append(label)
            recommended_caps = {
                label: max(1, current_caps.get(label, 5) // 2)
                for label in labels
            }
            provider = provider or provider_for(labels[0]) if labels else None
        fallback = PROVIDER_FALLBACK.get(provider or "", [])
        entry = {
            "provider": provider,
            "limit_pool_id": limit_pool_id,
            "budget_key": budget_key,
            "count": count,
            "labels": labels,
            "current_caps": {label: current_caps.get(label) for label in labels},
            "recommended_caps": recommended_caps,
            "fallback_providers": fallback,
        }
        out["providers_under_pressure"].append(entry)
    return out


def collect_records(state_dir: Path) -> list[dict]:
    """Read all dispatch records under <state_dir>/runs.d/."""
    runs = state_dir / "runs.d"
    if not runs.is_dir():
        return []
    out = []
    for path in sorted(runs.glob("*.json")):
        rec = _read_record(path)
        if rec is not None:
            out.append(rec)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect rate-limit pressure across dispatch ledger; emit recommendation JSON."
    )
    parser.add_argument(
        "--state-dir",
        default=str(_default_state_dir()),
        help="Goal-flight state directory (default: $GOALFLIGHT_STATE_DIR or /tmp/goal-flight-<uid>/)",
    )
    parser.add_argument(
        "--window-seconds",
        type=int,
        default=600,
        help="Rolling window for pressure detection (default 600s = 10min)",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=3,
        help="Rate-limit signatures per provider to declare pressure (default 3)",
    )
    parser.add_argument("--json", action="store_true", help="JSON output (default; here for parity)")
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir)
    records = collect_records(state_dir)
    billing = load_billing_accounts()
    pool_map = agent_limit_pool_map(billing)

    # Read current caps from goalflight_capacity if importable; fall back to a
    # safe default map. Importing avoids hard-coding the map in two places.
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from goalflight_capacity import DEFAULT_AGENT_CAPS  # type: ignore
        current_caps = dict(DEFAULT_AGENT_CAPS)
    except ImportError:
        current_caps = {label: 5 for label in AGENT_TO_PROVIDER}

    pressure = pressure_per_provider(records, window_seconds=args.window_seconds, pool_map=pool_map)
    payload = recommend(pressure, current_caps, threshold=args.threshold, pool_map=pool_map)
    payload["state_dir"] = str(state_dir)
    payload["window_seconds"] = args.window_seconds
    payload["records_examined"] = len(records)
    payload["limit_pool_map_loaded"] = bool(pool_map)
    print(json.dumps(payload, indent=2 if not args.json else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
