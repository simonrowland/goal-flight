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
  moonshot             moonshot (direct kimi CLI lane; legacy records: agent "kimi")
  cursor               cursor, cursor-agent (same Cursor subscription)

Two codex labels share OpenAI budget; two cursor labels share Cursor budget.
The walkback enforces ONE recommendation per provider regardless of how many
labels point at it.

Detection
---------
Rate-limit signatures vary by vendor. We scan record state + status-file
error fields for these case-insensitive patterns (mostly substring matches;
the Moonshot RPM signature uses its binary-verified bounded regex):

  - "rate_limit", "rate-limit", "rate limit"
  - HTTP-status-context "429"/"529" (not bare numbers in unrelated text)
  - "you've hit your limit", "usage limit"
  - "anthropic.RateLimitError", "openai.RateLimitError",
    "APIProviderRateLimitError" (kimi-code CLI class)
  - moonshot/kimi: "the engine is currently overloaded", "reached ... max rpm",
    envelope anchor "status code: 429"
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
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import goalflight_compat
import goalflight_dispatch_states
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
    "moonshot": "moonshot",
    # Legacy records carry agent "kimi" (retired handle); they mean the same
    # Moonshot budget. Input validation never sees this entry — only record
    # readers do.
    "kimi": "moonshot",
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
    # Kimi has one operator account, so Moonshot pressure must reroute across
    # providers rather than imply that another Kimi seat can absorb the work.
    "moonshot":           ["codex", "grok"],
    "cursor":            ["codex", "grok"],
}

ACCOUNT_RATE_LIMIT_SCOPE = "account_rate_limit"
MODEL_CAPACITY_SCOPE = "model_capacity"

# Both goalflight_dispatch._record_queued_ledger_fast and
# goalflight_ledger.cmd_record write this value when no --account was selected.
# It is an absence marker, unlike qualified billing keys such as
# "openai/default", which identify real configured accounts.
ACCOUNT_PLACEHOLDERS = frozenset({"default"})

# Billing account keys use vendor namespaces while pressure providers sometimes
# distinguish transports (for example Anthropic session vs CLI).  A namespace
# may therefore describe more than one pressure provider, but every record's
# agent label still selects exactly one provider-qualified account lane.
ACCOUNT_NAMESPACE_PROVIDERS: dict[str, frozenset[str]] = {
    "anthropic": frozenset({"anthropic-session", "anthropic-cli-acp", "anthropic-api"}),
    "cursor": frozenset({"cursor"}),
    "grok": frozenset({"xai"}),
    "moonshot": frozenset({"moonshot"}),
    "openai": frozenset({"openai"}),
    "xai": frozenset({"xai"}),
}


class LimitPattern(str):
    """String-compatible entry in the one authoritative signature table."""

    def __new__(
        cls,
        marker: str,
        kind: str,
        *,
        signature: str | None = None,
        scope: str = ACCOUNT_RATE_LIMIT_SCOPE,
        regex: re.Pattern[str] | None = None,
    ) -> "LimitPattern":
        value = str.__new__(cls, marker)
        value.kind = kind
        value.signature = signature or marker
        value.scope = scope
        value.regex = regex
        return value


class LimitEvidence(str):
    """Backward string-compatible signature carrying measured kind/evidence."""

    def __new__(
        cls,
        signature: str,
        *,
        kind: str,
        scope: str,
        reset_at: str | None,
        retry_after: float | None,
    ) -> "LimitEvidence":
        value = str.__new__(cls, signature)
        value.signature = signature
        value.kind = kind
        value.scope = scope
        value.state = goalflight_dispatch_states.limit_state_for_kind(kind)
        value.reset_at = reset_at
        value.retry_after = retry_after
        return value

    def get(self, key: str, default: object = None) -> object:
        aliases = {
            "signature": self.signature,
            "limit_signature": self.signature,
            "kind": self.kind,
            "limit_kind": self.kind,
            "scope": self.scope,
            "state": self.state,
            "reset_at": self.reset_at,
            "retry_after": self.retry_after,
        }
        return aliases.get(key, default)


E = goalflight_dispatch_states.LIMIT_KIND_EXHAUSTED
T = goalflight_dispatch_states.LIMIT_KIND_TRANSIENT
U = goalflight_dispatch_states.LIMIT_KIND_UNKNOWN

# One table decides signature, kind, and pressure scope. Entries are strings so
# existing low-level scanners can still use casefold()/substring operations.
# Specific evidence precedes generic umbrella phrases.
RATE_LIMIT_PATTERNS: tuple[LimitPattern, ...] = (
    LimitPattern("usage balance exhausted", E),
    LimitPattern("prepaid credits are depleted", E),
    LimitPattern("insufficient_quota", E),
    LimitPattern("quota exceeded", E),
    LimitPattern("weekly limit", E),
    LimitPattern("usage limit", E),
    LimitPattern("blocked_session_limit", E),
    LimitPattern("session_limit", E),
    LimitPattern("session limit", E),
    LimitPattern("payment_required", E),
    LimitPattern("payment required", E),
    *(
        LimitPattern(anchor, E, signature="402")
        for anchor in (
            "http 402", "status 402", "status: 402", "402 payment required",
            "got 402", "error 402", "(status 402", '"http_status": 402',
            '"http_status":402', '"code": 402', '"code":402',
        )
    ),
    LimitPattern("too many requests", T),
    LimitPattern("overloaded_error", T),
    LimitPattern("the engine is currently overloaded", T),
    LimitPattern("exceeded retry limit", T),
    *(
        LimitPattern(anchor, T, signature="429")
        for anchor in (
            "http 429", "status 429", "status: 429", "429 too many",
            "got 429", "error 429", '"code": 429', '"code":429',
            "status code: 429",
        )
    ),
    *(
        LimitPattern(anchor, T, signature="529")
        for anchor in (
            "http 529", "status 529", "status: 529", "529 overloaded",
            "got 529", "error 529", "(529)", '"code": 529', '"code":529',
        )
    ),
    LimitPattern(
        "reached ... max rpm",
        T,
        regex=re.compile(r"\breached\b[^\r\n]{0,200}\bmax rpm\b", re.IGNORECASE),
    ),
    LimitPattern("selected model is at capacity", T, scope=MODEL_CAPACITY_SCOPE),
    LimitPattern("model is at capacity", T, scope=MODEL_CAPACITY_SCOPE),
    LimitPattern("rate_limit", U),
    LimitPattern("rate-limit", U),
    LimitPattern("rate limit", U),
    LimitPattern("you've hit your limit", U),
    LimitPattern("try again at", U),
    LimitPattern("anthropic.ratelimiterror", U),
    LimitPattern("openai.ratelimiterror", U),
    LimitPattern("rate_limit_error", U),
    LimitPattern("resource_exhausted", U),
    LimitPattern("check your settings to continue", U),
    LimitPattern("apiproviderratelimiterror", U),
)
_CODEX_RESET_RE = re.compile(
    r"\b(?:try\s+again|reset(?:s)?)\s+(?:at|on)\s+"
    r"(?P<month>[A-Z][a-z]{2,8})\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?\s*,?\s+"
    r"(?P<year>\d{4})\s+"
    r"(?P<clock>\d{1,2}:\d{2}\s*[AP]M)\b",
    re.IGNORECASE,
)
_ISO_RESET_RE = re.compile(
    r"\b(?:try\s+again|reset(?:s)?(?:_at)?)\s*(?:at|on|[:=])\s*"
    r"(?P<iso>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2}))",
    re.IGNORECASE,
)
_RETRY_AFTER_RE = re.compile(
    r"\bretry(?:-|\s*)after\s*[:=]?\s*(?P<seconds>\d+(?:\.\d+)?)\s*(?:s|sec(?:ond)?s?)?\b",
    re.IGNORECASE,
)
_RETRY_IN_RE = re.compile(
    r"\bretry\s+in\s+(?P<seconds>\d+(?:\.\d+)?)\s*(?:s|sec(?:ond)?s?)\b",
    re.IGNORECASE,
)


def _reset_at_in_text(text: str) -> str | None:
    iso_match = _ISO_RESET_RE.search(text)
    if iso_match:
        value = iso_match.group("iso")
        if value.endswith(("Z", "z")):
            value = f"{value[:-1]}+00:00"
        try:
            return datetime.fromisoformat(value).isoformat()
        except ValueError:
            return None
    match = _CODEX_RESET_RE.search(text)
    if not match:
        return None
    value = (
        f"{match.group('month')} {match.group('day')}, "
        f"{match.group('year')} {match.group('clock')}"
    )
    try:
        # Codex renders this timestamp in the invoking machine's local zone.
        return datetime.strptime(value, "%b %d, %Y %I:%M %p").astimezone().isoformat()
    except ValueError:
        return None


def _retry_after_in_text(text: str) -> float | None:
    match = _RETRY_AFTER_RE.search(text) or _RETRY_IN_RE.search(text)
    return float(match.group("seconds")) if match else None


def rate_limit_signature_in_text(text: str) -> LimitEvidence | None:
    lowered = str(text or "").lower()
    retry_after = _retry_after_in_text(text)
    for pattern in RATE_LIMIT_PATTERNS:
        matched = pattern.regex.search(text) if pattern.regex else pattern in lowered
        if not matched:
            continue
        kind = pattern.kind
        if retry_after is not None and kind == U:
            kind = T
        return LimitEvidence(
            pattern.signature,
            kind=kind,
            scope=pattern.scope,
            reset_at=_reset_at_in_text(text),
            retry_after=retry_after,
        )
    return None


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


class AgentLimitPoolMap(dict):
    """Label pools plus account pools used to resolve dispatch records."""

    def __init__(self) -> None:
        super().__init__()
        self.account_pools: dict[str, str | None] = {}
        self.account_declared_keys: dict[str, str] = {}
        self.provider_accounts: dict[str, list[str]] = {}
        self.billing_accounts_present = False


def _account_budget_key(provider: str, account: str) -> str:
    """Return a collision-safe account key, normalizing a matching namespace."""
    local_account = account.strip()
    namespace, separator, suffix = local_account.partition("/")
    if separator and provider in ACCOUNT_NAMESPACE_PROVIDERS.get(namespace, frozenset()):
        local_account = suffix
    return f"account:{provider}:{local_account}"


def account_budget_key_for_agent(agent_label: str, account: str) -> str | None:
    """Qualify a local account alias by the provider selected by its agent."""
    provider = provider_for(agent_label)
    account = str(account or "").strip()
    if not provider or not account:
        return None
    return _account_budget_key(provider, account)


def agent_limit_pool_map(billing_doc: dict | None) -> AgentLimitPoolMap:
    """Map label pools and provider-qualified billing accounts."""
    out = AgentLimitPoolMap()
    if not billing_doc:
        return out
    accounts = billing_doc.get("accounts") or []
    out.billing_accounts_present = bool(accounts)
    for account in accounts:
        pool_id = account.get("limit_pool_id")
        account_key = account.get("account_key")
        labels = [str(label) for label in account.get("agent_labels") or []]
        if account_key:
            account_key = str(account_key)
            account_pool = str(pool_id) if pool_id else None
            providers = {provider_for(label) for label in labels}
            providers.discard(None)
            if not providers:
                namespace = account_key.partition("/")[0]
                providers.update(ACCOUNT_NAMESPACE_PROVIDERS.get(namespace, frozenset()))
            for provider in sorted(providers):
                qualified_key = _account_budget_key(provider, account_key)
                if qualified_key not in out.account_pools:
                    out.account_pools[qualified_key] = account_pool
                elif out.account_pools[qualified_key] != account_pool:
                    out.account_pools[qualified_key] = None
                out.account_declared_keys[qualified_key] = account_key
                declared = out.provider_accounts.setdefault(provider, [])
                if account_key not in declared:
                    declared.append(account_key)
        if not pool_id:
            continue
        pool_id = str(pool_id)
        for label in labels:
            if label not in out:
                out[label] = pool_id
            elif out[label] != pool_id:
                out[label] = None
    return out


def budget_key_for_agent(
    agent_label: str,
    *,
    pool_map: dict[str, str | None] | None = None,
) -> str | None:
    """Prefer limit_pool_id; keep multi-pool ambiguity off the full provider roster.

    When several billing accounts claim the same label, ``pool_map[label]`` is
    ``None``. Falling through to ``provider:<name>`` would group that pressure
    with *every* ``AGENT_TO_PROVIDER`` label for the provider — including labels
    no billing account listed (measured: ``codex`` + ``account=default`` pulled
    ``opencode*`` into the same cap set). Ambiguous declared labels use a
    distinct key that expands only to labels that actually declared the agent.
    """
    if pool_map is not None and agent_label in pool_map:
        pool = pool_map[agent_label]
        if pool:
            return f"pool:{pool}"
        # Declared in billing by multiple pools (value is None). Distinct key so
        # recommend() only attaches other multi-pool-declared labels for this
        # provider — never labels absent from billing entirely.
        provider = provider_for(agent_label)
        if provider:
            return f"provider-ambiguous:{provider}"
        return None
    provider = provider_for(agent_label)
    if provider:
        return f"provider:{provider}"
    return None


def _budget_key_for_record(
    record: dict,
    agent_label: str,
    *,
    pool_map: dict[str, str | None] | None,
) -> str | None:
    effective_account = str(record.get("effective_account") or "").strip()
    if effective_account in ACCOUNT_PLACEHOLDERS:
        effective_account = ""
    account = str(record.get("account") or "").strip()
    if account in ACCOUNT_PLACEHOLDERS:
        account = ""
    account = effective_account or account
    billing_accounts_present = getattr(pool_map, "billing_accounts_present", None)
    account_scoping_available = (
        bool(pool_map)
        if billing_accounts_present is None
        else bool(billing_accounts_present)
    )
    if account and account_scoping_available:
        return account_budget_key_for_agent(agent_label, account)
    return budget_key_for_agent(agent_label, pool_map=pool_map)


def budget_key_for_record(
    record: dict,
    agent_label: str,
    *,
    pool_map: dict[str, str | None] | None,
) -> str | None:
    """Public record-aware budget key used by both pressure channels."""
    return _budget_key_for_record(record, agent_label, pool_map=pool_map)


def _labels_for_account_key(
    budget_key: str,
    pool_map: dict[str, str | None] | None,
) -> tuple[list[str], str | None, str | None, str | None, dict[str, Any]]:
    """Resolve dispatch labels from the provider-qualified account lane."""
    parts = budget_key.split(":", 2)
    if len(parts) != 3 or parts[0] != "account" or not parts[1] or not parts[2]:
        return [], None, None, None, {
            "status": "unresolved",
            "reason": "account_budget_key_not_provider_qualified",
        }
    provider, account_key = parts[1], parts[2]
    labels = [label for label in AGENT_TO_PROVIDER if provider_for(label) == provider]
    if not labels:
        return [], None, provider, account_key, {
            "status": "unresolved",
            "reason": "no_dispatch_labels_for_account_provider",
            "provider": provider,
        }
    account_pools = getattr(pool_map, "account_pools", None)
    if account_pools is None:
        return labels, None, provider, account_key, {
            "status": "resolved_with_warning",
            "source": "budget_key_provider",
            "reason": "billing_account_map_unavailable",
            "provider": provider,
        }
    if budget_key not in account_pools:
        return labels, None, provider, account_key, {
            "status": "resolved_with_warning",
            "source": "budget_key_provider",
            "reason": "ledger_account_not_declared_in_billing",
            "provider": provider,
            "ledger_account_key": account_key,
            "declared_account_keys": list(
                getattr(pool_map, "provider_accounts", {}).get(provider, [])
            ),
        }
    pool_id = account_pools[budget_key]
    if not pool_id:
        return labels, None, provider, account_key, {
            "status": "resolved_with_warning",
            "source": "billing_account_provider",
            "reason": "account_limit_pool_unresolved",
            "provider": provider,
        }
    return labels, pool_id, provider, account_key, {
        "status": "resolved",
        "source": "billing_account_provider",
        "provider": provider,
        "declared_account_key": getattr(pool_map, "account_declared_keys", {}).get(budget_key),
    }


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


def _pressure_state(record: dict, status: dict | None) -> str:
    """Resolve the state used for pressure gating.

    Ledger terminal states win. When the ledger is still non-terminal (e.g.
    background watcher finalize failed) but the status plane already reached a
    terminal failure, fall back to the status state so caps are not silently
    under-counted. Successful terminal status (complete) never upgrades a
    running ledger into pressure.
    """
    record_state = record.get("state")
    record_lower = str(record_state or "").lower()
    if goalflight_dispatch_states.is_terminal_state(record_state):
        return record_lower
    if not status:
        return record_lower
    status_state = status.get("state")
    if not goalflight_dispatch_states.is_terminal_state(status_state):
        return record_lower
    normalized = goalflight_dispatch_states.normalize_dispatch_state(status_state)
    if normalized == "complete":
        return record_lower
    return (normalized or str(status_state or "")).lower()


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
    reason = status.get("reason")
    if reason:
        if isinstance(reason, dict):
            haystack_parts.append(json.dumps(reason))
        else:
            haystack_parts.append(str(reason))
    return " ".join(haystack_parts).lower()


def detect_pressure_scope(record: dict, status: dict | None) -> str | None:
    """Return the pressure scope for this dispatch, or None.

    Checks: record.state (goal-flight's classification), status.error fields,
    and status.text_excerpt for vendor-specific patterns.

    NOTE: `blocked_auth` is deliberately NOT counted as rate-limit pressure.
    Auth failures are provider-availability problems (missing/invalid
    credentials) that need credential repair, not cap-halving. Counting them
    here would trigger walkback recommendations that mask the real fix.
    """
    state = _pressure_state(record, status)
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
    if not goalflight_dispatch_states.is_limit_state(state) and state not in {
        "failed",
        "inconclusive_timeout",
        "blocked",
        "inconclusive_no_final",
        "worker_dead",
    }:
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
    evidence = rate_limit_signature_in_text(haystack)
    return evidence.scope if evidence is not None else None


def detect_rate_limit_signature(record: dict, status: dict | None) -> bool:
    """Return True if this dispatch shows pressure signs."""
    return detect_pressure_scope(record, status) is not None


def pressure_per_provider(
    records: list[dict],
    window_seconds: int = 600,
    now_ts: float | None = None,
    *,
    pool_map: dict[str, str | None] | None = None,
) -> dict[str, int]:
    """Count pressure signatures per budget key within window.

    Real account-bearing records use `account:<provider>:<local_account>`.
    Placeholder or absent account values use the legacy label-derived
    pool/provider key. When billing facts are unavailable, all records retain
    that legacy behavior.
    Model-capacity signals use `agent:<label>` because they are not account-wide
    quota signals.
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
            key = _budget_key_for_record(record, str(agent), pool_map=pool_map)
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
    return counts


def recommend(
    pressure: dict[str, int],
    current_caps: dict[str, int],
    threshold: int = 3,
    *,
    pool_map: dict[str, str | None] | None = None,
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
        labels = list(label_groups.get(budget_key, []))
        scope = "provider"
        provider = None
        account_key = None
        label_resolution = None
        limit_pool_id = budget_key.split(":", 1)[1] if budget_key.startswith("pool:") else None
        if budget_key.startswith("account:"):
            scope = "account"
            labels, limit_pool_id, provider, account_key, label_resolution = _labels_for_account_key(
                budget_key,
                pool_map,
            )
        if budget_key.startswith("agent:"):
            scope = "agent"
            agent_label = budget_key.split(":", 1)[1]
            labels = labels or [agent_label]
        if budget_key.startswith("provider-ambiguous:"):
            # Multi-pool-declared labels only (built via budget_key_for_agent).
            # Capacity may still act on these label caps; labels never listed in
            # billing do not share this key.
            provider = budget_key.split(":", 1)[1]
        elif budget_key.startswith("provider:"):
            provider = budget_key.split(":", 1)[1]
        if limit_pool_id and pool_map and not account_key:
            for label, pool in pool_map.items():
                if pool == limit_pool_id and label not in labels:
                    labels.append(label)
            provider = provider or provider_for(labels[0]) if labels else None
        # Account-scoped pressure has no capacity actuator (leases are label/pool
        # keyed). Emitting cap-shaped recommended_caps would look actuatable to
        # doctor/status while adaptive_agent_cap deliberately ignores the entry.
        if scope == "account":
            recommended_caps: dict[str, int] = {}
        else:
            recommended_caps = {
                label: max(1, current_caps.get(label, 5) // 2)
                for label in labels
            }
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
        if account_key is not None:
            entry["account_key"] = account_key
        if label_resolution is not None:
            entry["label_resolution"] = label_resolution
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
