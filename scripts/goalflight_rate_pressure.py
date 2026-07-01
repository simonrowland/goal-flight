#!/usr/bin/env python3
"""Adaptive rate-limit pressure detector for goal-flight.

Reads the dispatch ledger and recent worker status files, classifies failures
into account rate-limit pressure per provider or model capacity per label, and emits a JSON recommendation the
orchestrator reads before its next dispatch decision. Read-only in v1 — the
orchestrator decides whether to act; this script never mutates capacity state.

Provider model
--------------
Per-label caps in `goalflight_agent_limits.DEFAULT_AGENT_CAPS` are PROCESS-COUNT
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
  - HTTP-status-context "429"/"529" (not bare numbers in unrelated text)
  - "you've hit your limit", "usage limit"
  - "anthropic.RateLimitError", "openai.RateLimitError"
  - "session_limit"
  - "blocked_session_limit"  (goal-flight's own classification)
  - "Selected model is at capacity" / "model is at capacity" (label-scoped)

Pressure rule
-------------
DEFAULT: 3+ rate-limit signatures for the same provider within the last 600s
(10 minutes) = under pressure. Tune via env / flags.

Recommendation
--------------
For each provider or model label under pressure:
  - reduce that provider's or label's effective cap by 50% (floor 1)
  - re-route task categories the SKILL.md routing table defaults onto that
    provider toward the documented fallback

The orchestrator/capacity gate reads this JSON, surfaces a STATUS marker to the
user, and optionally re-routes its next dispatch. Mutation of capacity state is
explicitly out of scope — keep the policy human-supervisable.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import goalflight_compat
from goalflight_agent_limits import DEFAULT_AGENT_CAPS

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
    "grok-acp": "xai",
    "grok-code": "xai",
    "grok-research": "xai",
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

# Substring patterns indicating an account/provider rate-limit failure.
# Case-insensitive.
RATE_LIMIT_PATTERNS: tuple[str, ...] = (
    "rate_limit",
    "rate-limit",
    "rate limit",
    "you've hit your limit",
    "usage limit",
    "anthropic.ratelimiterror",
    "openai.ratelimiterror",
    "session_limit",
    "blocked_session_limit",
    # Coverage-audit additions (2026-06-10) — provider phrases the original
    # list missed. Matching stays failure-state-gated (detect_pressure_scope),
    # so prompt-echo inside successful runs cannot false-positive.
    "too many requests",        # OpenAI/HTTP prose form of 429
    "insufficient_quota",       # OpenAI hard quota
    "rate_limit_error",         # Anthropic API error type
    "overloaded_error",         # Anthropic 529 error type
    "session limit",            # Claude CLI prose (space form; underscore form above)
    "resource_exhausted",       # gRPC/generic quota signal
    "quota exceeded",           # generic billing/quota hard limit
    "check your settings to continue",  # Cursor plan/usage block (full phrase for precision)
    # codex CLI + xAI/grok additions. NOTE: most
    # codex/xAI 429 PROSE ("you've exceeded the rate limit", "rate limit reached for
    # organization", xAI "...reached the rate limit") is ALREADY caught by the
    # substrings above ("rate limit"/"too many requests"), so only the
    # genuinely-uncaught literals are added. Matching stays failure-state-gated.
    "exceeded retry limit",          # codex CLI retry-wrapper exhaustion (catches retry-exhaust even when the line is not a bare 429)
    "prepaid credits are depleted",  # xAI hard CREDIT exhaustion — a distinct failure mode (no 429, no "rate limit" text)
    "payment_required",              # xAI 402 credits-out (code form; safer than a bare "402")
    # xAI Grok Build usage-balance exhaustion — OBSERVED LIVE 2026-07-01. The real
    # grok CLI-proxy text is: 'API error (status 402 Payment Required): Grok Build
    # usage balance exhausted' (Request URL cli-chat-proxy.grok.com). Neither
    # "payment_required" (underscore) NOR any 402 anchor (there was none) matched it,
    # so rate_limit_signature_in_text() returned None: adaptive walk-back never fired,
    # a whole grok batch failed on 402 and the workers hung (orphan leak), all while
    # the monitor read pressure=none. Add the actual prose + HTTP reason-phrase form.
    "usage balance exhausted",       # xAI Grok Build balance gone (prose; unambiguous)
    "payment required",              # HTTP 402 reason-phrase (space form) as emitted by the grok CLI proxy
)

# HTTP status codes require provider-error context — bare "429"/"529" in line
# numbers, ids, or unrelated prose must not false-positive.
RATE_LIMIT_HTTP_STATUS_ANCHORS: dict[str, tuple[str, ...]] = {
    "429": (
        "http 429",
        "status 429",
        "status: 429",
        "429 too many",
        "got 429",
        "error 429",
        '"code": 429',
        '"code":429',
    ),
    "529": (
        "http 529",
        "status 529",
        "status: 529",
        "529 overloaded",
        "got 529",
        "error 529",
        "(529)",
        '"code": 529',
        '"code":529',
    ),
    # 402 Payment Required — xAI hard usage/credit exhaustion (added 2026-07-01,
    # see RATE_LIMIT_PATTERNS note). 402 is unambiguously a billing/quota wall, so
    # anchoring on the status is safe.
    "402": (
        "http 402",
        "status 402",
        "status: 402",
        "402 payment required",
        "got 402",
        "error 402",
        "(status 402",
        '"http_status": 402',
        '"http_status":402',
        '"code": 402',
        '"code":402',
    ),
}


def rate_limit_signature_in_text(text: str) -> str | None:
    lowered = text.lower()
    for pattern in RATE_LIMIT_PATTERNS:
        if pattern in lowered:
            return pattern
    return None

# Substring patterns indicating model-specific capacity, not account-wide quota.
# These reduce only the label that produced the signal. Bare "at capacity" is
# excluded — unrelated prose can mention capacity without a model-scoped signal.
MODEL_CAPACITY_PATTERNS: tuple[str, ...] = (
    "selected model is at capacity",
    "model is at capacity",
)

ACCOUNT_RATE_LIMIT_SCOPE = "account_rate_limit"
MODEL_CAPACITY_SCOPE = "model_capacity"


def provider_for(agent_label: str) -> str | None:
    """Map an agent label to its provider key. Returns None for unknown labels."""
    return AGENT_TO_PROVIDER.get(agent_label)


def default_fleet_dir() -> Path:
    return goalflight_compat.resolve_env_path(
        "GOALFLIGHT_FLEET_DIR", Path.home() / ".goal-flight" / "fleet"
    )


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
    return goalflight_compat.resolve_state_dir()


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


def _status_haystack(status: dict | None) -> str:
    if not status:
        return ""
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
    # Coverage audit 2026-06-10: result_text carries the worker's final reply,
    # which on failure-state dispatches often holds the provider's limit prose
    # (text_excerpt can truncate it away). Safe to scan: detect_pressure_scope
    # state-gates this haystack to failure-ish records only.
    result_text = status.get("result_text")
    if result_text:
        haystack_parts.append(str(result_text))
    return " ".join(haystack_parts).lower()


def _haystack_matches_rate_limit(haystack: str) -> bool:
    """True when haystack carries account/provider rate-limit evidence."""
    if any(pat in haystack for pat in RATE_LIMIT_PATTERNS):
        return True
    for status_code, anchors in RATE_LIMIT_HTTP_STATUS_ANCHORS.items():
        if any(anchor in haystack for anchor in anchors):
            return True
        # overloaded_error already lives in RATE_LIMIT_PATTERNS; keep 529 tied to it.
        if status_code == "529" and "overloaded_error" in haystack:
            return True
    return False


def detect_pressure_scope(record: dict, status: dict | None) -> str | None:
    """Return the pressure scope for this dispatch, or None.

    Checks: record.state (goal-flight's classification), status.error fields,
    and status.text_excerpt for vendor-specific patterns.

    NOTE: `blocked_auth` is deliberately NOT counted as rate-limit pressure.
    Auth failures are provider-availability problems (missing/invalid
    credentials) that need credential repair, not cap-halving. Counting them
    here would trigger walkback recommendations that mask the real fix.
    """
    state = (record.get("state") or "").lower()
    if state == "blocked_session_limit":
        return ACCOUNT_RATE_LIMIT_SCOPE
    # Failure-ish states whose error text deserves a pattern scan (coverage
    # audit 2026-06-10 widened this from {failed, inconclusive_timeout};
    # dispatch death-classification wiring later added worker_dead because
    # launcher/watcher failures may be the only place provider limit prose is
    # preserved).
    # DELIBERATELY EXCLUDED: "blocked_capacity" is goal-flight's OWN capacity
    # gate — counting it would feed our queueing back into the walk-back and
    # falsely halve provider caps (self-referential pressure). "blocked_auth"
    # stays excluded per the note above.
    if state not in {"failed", "inconclusive_timeout", "blocked", "inconclusive_no_final", "worker_dead"}:
        # Successful, pending, capacity-, or auth-blocked dispatches don't count.
        return None

    haystack = _status_haystack(status)
    # The ledger record's own error field is a second signal carrier the status
    # file may lack (e.g. spawn-path failures) — coverage audit 2026-06-10.
    record_error = record.get("error")
    if record_error:
        haystack = f"{haystack} {str(record_error).lower()}".strip()
    if not haystack:
        return None
    # Account-wide rate limits take precedence when both signals appear — mixed
    # HTTP 429 + model-capacity prose is still provider quota pressure.
    if _haystack_matches_rate_limit(haystack):
        return ACCOUNT_RATE_LIMIT_SCOPE
    if any(pat in haystack for pat in MODEL_CAPACITY_PATTERNS):
        return MODEL_CAPACITY_SCOPE
    return None


def detect_rate_limit_signature(record: dict, status: dict | None) -> bool:
    """Return True if this dispatch shows pressure signs."""
    return detect_pressure_scope(record, status) is not None


def pressure_per_provider(
    records: list[dict],
    window_seconds: int = 600,
    now_ts: float | None = None,
    *,
    pool_map: dict[str, str] | None = None,
) -> dict[str, int]:
    """Count pressure signatures per budget key within window.

    Keys are `pool:<limit_pool_id>` when fleet billing map is available,
    otherwise `provider:<provider_key>`. Model-capacity signals use
    `agent:<label>` because they are not account-wide quota signals.
    """
    if now_ts is None:
        now_ts = time.time()
    cutoff_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now_ts - window_seconds))
    counts: dict[str, int] = {}
    for record in records:
        agent = record.get("agent")
        if not agent:
            continue
        updated = record.get("updated_at") or record.get("started_at") or ""
        if not updated or updated < cutoff_iso:
            continue
        status = _read_status(record)
        scope = detect_pressure_scope(record, status)
        if scope is None:
            continue
        if scope == MODEL_CAPACITY_SCOPE:
            key = f"agent:{str(agent).strip().lower()}"
        else:
            key = budget_key_for_agent(agent, pool_map=pool_map)
        if not key:
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
        label_groups.setdefault(f"agent:{agent_label}", []).append(agent_label)

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
        scope = "provider"
        if budget_key.startswith("agent:"):
            scope = "agent"
            agent_label = budget_key.split(":", 1)[1]
            labels = labels or [agent_label]
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
        if scope == "agent":
            provider = provider_for(labels[0]) if labels else provider
        fallback = PROVIDER_FALLBACK.get(provider or "", [])
        entry = {
            "scope": scope,
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

    current_caps = dict(DEFAULT_AGENT_CAPS)

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
