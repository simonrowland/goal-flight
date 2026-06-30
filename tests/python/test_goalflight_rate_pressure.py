"""Tests for scripts/goalflight_rate_pressure.py — adaptive rate-limit walkback.

Test surface covers:
- provider_for() agent → provider mapping, including aliasing (cursor +
  cursor-agent collapse to "cursor"; codex + codex-acp collapse to "openai").
- detect_rate_limit_signature() across vendor error shapes and goal-flight's
  own state classifications (blocked_session_limit / blocked_auth / failed
  with rate-limit text in status).
- pressure_per_provider() windowing — records older than the window are
  excluded; multiple labels for the same provider sum correctly.
- recommend() — provider-level cap halving (floor 1), fallback-provider list
  populated, only providers at-or-above threshold appear; model-capacity
  pressure stays label-scoped.
- collect_records() reads a tmp state dir cleanly.
"""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("rate-pressure fixtures assert POSIX /tmp state paths")

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_rate_pressure as rp  # noqa: E402


def assert_eq(name, got, expected):
    if got != expected:
        raise AssertionError(f"{name}: got {got!r}, expected {expected!r}")


def assert_true(name, cond):
    if not cond:
        raise AssertionError(name)


# ----- provider_for() -----

def test_provider_for_known_aliases():
    """cursor/cursor-agent → cursor; codex/codex-acp → openai; etc."""
    assert_eq("cursor → cursor", rp.provider_for("cursor"), "cursor")
    assert_eq("cursor-agent → cursor", rp.provider_for("cursor-agent"), "cursor")
    assert_eq("codex → openai", rp.provider_for("codex"), "openai")
    assert_eq("codex-acp → openai", rp.provider_for("codex-acp"), "openai")
    assert_eq("grok → xai", rp.provider_for("grok"), "xai")
    assert_eq("claude → anthropic-session", rp.provider_for("claude"), "anthropic-session")
    assert_eq("claude-code-cli-acp → anthropic-cli-acp",
              rp.provider_for("claude-code-cli-acp"), "anthropic-cli-acp")


def test_provider_for_unknown():
    """Unknown labels return None (caller skips them)."""
    assert_eq("unknown label", rp.provider_for("future-worker-9000"), None)


def test_provider_for_bash_tail_variants():
    """bash-tail labels emitted by watch-dispatch-tail.sh map to the same
    provider as their ACP/Agent equivalents — same vendor budget, different
    dispatch shape. claude-bash-tail specifically maps to anthropic-api
    (claude -p is API-billed, separate from session)."""
    assert_eq("claude-bash-tail → anthropic-api",
              rp.provider_for("claude-bash-tail"), "anthropic-api")
    assert_eq("codex-bash-tail → openai",
              rp.provider_for("codex-bash-tail"), "openai")
    assert_eq("grok-bash-tail → xai",
              rp.provider_for("grok-bash-tail"), "xai")
    assert_eq("opencode → openai",
              rp.provider_for("opencode"), "openai")
    assert_eq("opencode-acp → openai",
              rp.provider_for("opencode-acp"), "openai")
    assert_eq("opencode-bash-tail → openai",
              rp.provider_for("opencode-bash-tail"), "openai")


# ----- detect_rate_limit_signature() -----

def test_detect_blocked_session_limit_state():
    """goal-flight's own classification triggers detection even without status payload."""
    record = {"agent": "claude", "state": "blocked_session_limit"}
    assert_true("blocked_session_limit detected from state alone",
                rp.detect_rate_limit_signature(record, None))


def test_detect_blocked_auth_state_does_not_trigger():
    """blocked_auth is intentionally NOT counted as rate pressure.

    Codex r3 review (2026-05-19) flagged that auth/config failures need
    credential repair, not cap-halving. The walkback's recommendation
    would mask the real fix. Keep blocked_auth out of the rate-limit bucket.
    """
    record = {"agent": "codex", "state": "blocked_auth"}
    assert_eq("blocked_auth NOT detected as rate pressure",
              rp.detect_rate_limit_signature(record, None), False)


def test_detect_failed_with_rate_limit_error():
    """Failed state plus rate-limit substring in status.error triggers."""
    record = {"agent": "claude", "state": "failed"}
    status = {"error": {"code": 429, "message": "rate_limit_exceeded"}}
    assert_true("failed + rate_limit_exceeded", rp.detect_rate_limit_signature(record, status))


def test_detect_failed_with_anthropic_signature():
    """anthropic.RateLimitError pattern."""
    record = {"agent": "claude", "state": "failed"}
    status = {"error": "anthropic.RateLimitError: too many requests"}
    assert_true("anthropic.RateLimitError detected", rp.detect_rate_limit_signature(record, status))


def test_detect_failed_with_usage_limit_text_excerpt():
    """The status.text_excerpt path is also scanned."""
    record = {"agent": "claude", "state": "failed"}
    status = {"text_excerpt": "You've hit your limit — resets at 2am."}
    assert_true("usage-limit text in excerpt", rp.detect_rate_limit_signature(record, status))


def test_detect_failed_with_codex_model_capacity_message():
    """Codex's selected-model capacity failure is label-scoped pressure."""
    record = {"agent": "codex", "state": "failed"}
    status = {"error": "ERROR: Selected model is at capacity. Please try a different model."}
    assert_true("codex model capacity detected", rp.detect_rate_limit_signature(record, status))
    assert_eq("codex model capacity scope",
              rp.detect_pressure_scope(record, status), rp.MODEL_CAPACITY_SCOPE)


def test_detect_failed_unrelated_error_does_not_trigger():
    """Failed state with non-rate-limit error doesn't trigger."""
    record = {"agent": "codex", "state": "failed"}
    status = {"error": "TypeError: object has no attribute 'foo'"}
    assert_eq("unrelated TypeError", rp.detect_rate_limit_signature(record, status), False)


def test_detect_complete_state_does_not_trigger():
    """Successful dispatches never trigger, regardless of status content."""
    record = {"agent": "claude", "state": "complete"}
    status = {"text_excerpt": "tested rate_limit handler — passed"}
    assert_eq("complete state ignored", rp.detect_rate_limit_signature(record, status), False)


def test_detect_inconclusive_timeout_with_rate_limit_status():
    """inconclusive_timeout + rate-limit excerpt does trigger (worker stalled mid-limit)."""
    record = {"agent": "claude", "state": "inconclusive_timeout"}
    status = {"text_excerpt": "Got 429 from API, retrying..."}
    assert_true("inconclusive_timeout + 429", rp.detect_rate_limit_signature(record, status))


def test_detect_worker_dead_with_rate_limit_record_error():
    """worker_dead + ledger error text triggers after dispatch tail enrichment."""
    record = {"agent": "codex", "state": "worker_dead", "error": {"tail_excerpt": "usage limit; try again later"}}
    assert_eq(
        "worker_dead rate-limit scope",
        rp.detect_pressure_scope(record, None),
        rp.ACCOUNT_RATE_LIMIT_SCOPE,
    )


def test_detect_rate_limited_complete_exit_record_error():
    """A complete/exit-0 dispatch reclassified as rate_limited remains visible."""
    record = {
        "agent": "codex",
        "state": "rate_limited",
        "error": {
            "message": "dispatch_worker_rate_limited",
            "reason": "marker:COMPLETE",
            "tail_excerpt": "You've hit your usage limit. Please try again at 6:13 AM.",
        },
    }
    assert_eq(
        "rate_limited complete-exit signal",
        rp.detect_pressure_scope(record, None),
        rp.ACCOUNT_RATE_LIMIT_SCOPE,
    )


def test_detect_try_again_at_pattern():
    record = {"agent": "codex", "state": "failed"}
    status = {"error": "You've hit your usage limit. Please try again at 6:13 AM."}
    assert_true("try again at pattern", rp.detect_rate_limit_signature(record, status))


# ----- pressure_per_provider() -----

def _build_record(agent, state, updated_at, dispatch_id="d"):
    return {
        "dispatch_id": dispatch_id,
        "agent": agent,
        "state": state,
        "updated_at": updated_at,
        "status_path": None,
    }


def test_pressure_groups_aliased_labels():
    """3 records: codex (1), codex-acp (2) → openai provider gets count 3."""
    now = time.time()
    recent_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 60))
    records = [
        _build_record("codex", "blocked_session_limit", recent_iso, "d1"),
        _build_record("codex-acp", "blocked_session_limit", recent_iso, "d2"),
        _build_record("codex-acp", "blocked_session_limit", recent_iso, "d3"),
    ]
    counts = rp.pressure_per_provider(records, window_seconds=600, now_ts=now)
    assert_eq("openai counts both labels", counts.get("provider:openai"), 3)


def test_pressure_outside_window_excluded():
    """Records older than window_seconds are not counted."""
    now = time.time()
    old_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 1800))  # 30 min ago
    recent_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 60))
    records = [
        _build_record("claude", "blocked_session_limit", old_iso, "d1"),
        _build_record("claude", "blocked_session_limit", recent_iso, "d2"),
    ]
    counts = rp.pressure_per_provider(records, window_seconds=600, now_ts=now)
    assert_eq("anthropic-session window-filtered", counts.get("provider:anthropic-session"), 1)


def test_pressure_only_failures_counted():
    """Successful records don't add to pressure count."""
    now = time.time()
    recent_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 60))
    records = [
        _build_record("claude", "complete", recent_iso, "d1"),
        _build_record("claude", "complete", recent_iso, "d2"),
        _build_record("claude", "blocked_session_limit", recent_iso, "d3"),
    ]
    counts = rp.pressure_per_provider(records, window_seconds=600, now_ts=now)
    assert_eq("only blocked_session_limit counted", counts.get("provider:anthropic-session"), 1)


def test_pressure_missing_agent_field():
    """Records with missing agent field are skipped (not crash)."""
    now = time.time()
    recent_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 60))
    records = [
        {"dispatch_id": "d1", "state": "blocked_session_limit", "updated_at": recent_iso},
        {"dispatch_id": "d2", "agent": None, "state": "blocked_session_limit", "updated_at": recent_iso},
        _build_record("claude", "blocked_session_limit", recent_iso, "d3"),
    ]
    counts = rp.pressure_per_provider(records, window_seconds=600, now_ts=now)
    assert_eq("only the one valid record counted", counts.get("provider:anthropic-session"), 1)


def test_pressure_started_at_fallback():
    """When updated_at is absent, started_at is used for windowing."""
    now = time.time()
    recent_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 60))
    records = [
        {"dispatch_id": "d1", "agent": "claude", "state": "blocked_session_limit", "started_at": recent_iso},
    ]
    counts = rp.pressure_per_provider(records, window_seconds=600, now_ts=now)
    assert_eq("started_at fallback works", counts.get("provider:anthropic-session"), 1)


def test_pressure_mixed_providers_in_window():
    """Multiple providers in same window are counted independently."""
    now = time.time()
    recent_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 60))
    records = [
        _build_record("claude", "blocked_session_limit", recent_iso, "d1"),
        _build_record("claude", "blocked_session_limit", recent_iso, "d2"),
        _build_record("codex", "blocked_session_limit", recent_iso, "d3"),
        _build_record("grok", "blocked_session_limit", recent_iso, "d4"),
    ]
    counts = rp.pressure_per_provider(records, window_seconds=600, now_ts=now)
    assert_eq("anthropic-session", counts.get("provider:anthropic-session"), 2)
    assert_eq("openai", counts.get("provider:openai"), 1)
    assert_eq("xai", counts.get("provider:xai"), 1)


def test_pressure_model_capacity_is_label_scoped():
    """Model-capacity failures count for the failing label, not its provider."""
    now = time.time()
    recent_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 60))
    state_dir = Path("/tmp") / f"goal-flight-test-model-capacity-{time.time_ns()}"
    status_dir = state_dir / "dispatch"
    status_dir.mkdir(parents=True, exist_ok=True)
    try:
        records = []
        for idx in range(3):
            status_path = status_dir / f"codex-{idx}.status.json"
            status_path.write_text(json.dumps({
                "error": "ERROR: Selected model is at capacity. Please try a different model.",
            }))
            records.append({
                "dispatch_id": f"codex-{idx}",
                "agent": "codex",
                "state": "failed",
                "updated_at": recent_iso,
                "status_path": str(status_path),
            })
        counts = rp.pressure_per_provider(records, window_seconds=600, now_ts=now)
        assert_eq("codex model capacity label key", counts.get("agent:codex"), 3)
        assert_eq("codex model capacity not provider-wide", counts.get("provider:openai"), None)
    finally:
        for p in status_dir.glob("*.json"):
            p.unlink()
        status_dir.rmdir()
        state_dir.rmdir()


# ----- recommend() -----

def test_recommend_below_threshold_empty():
    """Provider counts below threshold don't appear in providers_under_pressure."""
    out = rp.recommend({"provider:openai": 2}, {"codex": 10, "codex-acp": 10}, threshold=3)
    assert_eq("no providers under pressure", out["providers_under_pressure"], [])
    assert_eq("but providers_observed populated", out["providers_observed"], ["provider:openai"])


def test_recommend_above_threshold_halves_caps():
    """At threshold, recommended cap is current // 2 (floor 1)."""
    out = rp.recommend(
        {"provider:openai": 5},
        {
            "codex": 10,
            "codex-acp": 10,
            "codex-bash-tail": 10,
            "opencode": 10,
            "opencode-acp": 10,
            "opencode-bash-tail": 10,
        },
        threshold=3,
    )
    assert_eq("one provider", len(out["providers_under_pressure"]), 1)
    pup = out["providers_under_pressure"][0]
    assert_eq("provider key", pup["provider"], "openai")
    assert_eq("budget key", pup["budget_key"], "provider:openai")
    assert_eq("openai labels include bash-tail variants",
              sorted(pup["labels"]),
              ["codex", "codex-acp", "codex-bash-tail",
               "opencode", "opencode-acp", "opencode-bash-tail"])
    assert_eq("codex halved", pup["recommended_caps"]["codex"], 5)
    assert_eq("codex-acp halved", pup["recommended_caps"]["codex-acp"], 5)
    assert_eq("codex-bash-tail halved", pup["recommended_caps"]["codex-bash-tail"], 5)
    assert_eq("opencode halved", pup["recommended_caps"]["opencode"], 5)
    assert_eq("opencode-acp halved", pup["recommended_caps"]["opencode-acp"], 5)
    assert_eq("opencode-bash-tail halved", pup["recommended_caps"]["opencode-bash-tail"], 5)


def test_recommend_model_capacity_halves_only_affected_label():
    """agent:<label> pressure must not throttle sibling OpenAI labels."""
    out = rp.recommend(
        {"agent:codex": 3},
        {
            "codex": 10,
            "codex-acp": 10,
            "codex-bash-tail": 10,
            "opencode": 10,
            "opencode-acp": 10,
            "opencode-bash-tail": 10,
        },
        threshold=3,
    )
    assert_eq("one label pressure entry", len(out["providers_under_pressure"]), 1)
    pup = out["providers_under_pressure"][0]
    assert_eq("label pressure scope", pup["scope"], "agent")
    assert_eq("label budget key", pup["budget_key"], "agent:codex")
    assert_eq("provider retained for fallback context", pup["provider"], "openai")
    assert_eq("only codex label included", pup["labels"], ["codex"])
    assert_eq("only codex recommended cap", pup["recommended_caps"], {"codex": 5})


def test_recommend_cap_floor_one():
    """Caps already at 1 don't go to 0."""
    out = rp.recommend(
        {"provider:cursor": 4},
        {"cursor": 1, "cursor-agent": 1},
        threshold=3,
    )
    pup = out["providers_under_pressure"][0]
    assert_eq("cursor floor 1", pup["recommended_caps"]["cursor"], 1)
    assert_eq("cursor-agent floor 1", pup["recommended_caps"]["cursor-agent"], 1)


def test_recommend_fallback_providers_populated():
    """Each pressured provider includes the documented fallback chain."""
    out = rp.recommend({"provider:anthropic-session": 5}, {"claude": 5}, threshold=3)
    pup = out["providers_under_pressure"][0]
    fallback = pup["fallback_providers"]
    assert_true("fallback list non-empty", len(fallback) > 0)
    assert_true("fallback contains codex", "codex" in fallback)


def test_limit_pool_pressure_aggregation(tmp_path: Path | None = None):
    """Fleet billing map groups agent labels under limit_pool_id."""
    billing = {
        "schema": "goalflight.fleet.billing-accounts.v1",
        "schema_version": 1,
        "min_reader_version": 1,
        "accounts": [
            {
                "account_key": "openai/default",
                "limit_pool_id": "openai-default",
                "agent_labels": ["codex", "codex-acp"],
            }
        ],
    }
    pool_map = rp.agent_limit_pool_map(billing)
    now = time.time()
    recent_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 60))
    records = [
        _build_record("codex", "blocked_session_limit", recent_iso, "d1"),
        _build_record("codex-acp", "blocked_session_limit", recent_iso, "d2"),
    ]
    counts = rp.pressure_per_provider(records, window_seconds=600, now_ts=now, pool_map=pool_map)
    assert_eq("pool aggregation", counts.get("pool:openai-default"), 2)


# ----- collect_records() -----

def test_collect_records_empty_state_dir(tmp_path: Path | None = None):
    """Missing runs.d directory returns empty list, not crash."""
    state_dir = Path("/tmp") / f"goal-flight-test-empty-{int(time.time())}"
    # Don't create runs.d at all.
    assert_eq("no runs.d → empty list", rp.collect_records(state_dir), [])


def test_collect_records_reads_files():
    """Multiple JSON records under runs.d/ are read in sorted order."""
    state_dir = Path("/tmp") / f"goal-flight-test-collect-{int(time.time())}"
    runs = state_dir / "runs.d"
    runs.mkdir(parents=True, exist_ok=True)
    try:
        (runs / "a.json").write_text(json.dumps({"dispatch_id": "a", "agent": "codex"}))
        (runs / "b.json").write_text(json.dumps({"dispatch_id": "b", "agent": "claude"}))
        records = rp.collect_records(state_dir)
        assert_eq("two records", len(records), 2)
        agents = [r["agent"] for r in records]
        assert_eq("sorted by filename", agents, ["codex", "claude"])
    finally:
        for p in runs.glob("*.json"):
            p.unlink()
        runs.rmdir()
        state_dir.rmdir()


# ----- runner -----

def test_coverage_audit_patterns_states_and_guards():
    """2026-06-10 coverage-audit fold: provider phrases, widened failure
    states, record.error + result_text carriers, and the self-referential
    blocked_capacity guard."""
    S = rp.detect_pressure_scope
    A = rp.ACCOUNT_RATE_LIMIT_SCOPE
    # new provider phrases classify on failure states
    assert_eq("429 prose", S({"state": "failed"}, {"error": "HTTP 429 Too Many Requests"}), A)
    assert_eq("insufficient_quota", S({"state": "failed"}, {"text_excerpt": "insufficient_quota: billing"}), A)
    assert_eq("overloaded blocked-state", S({"state": "blocked"}, {"text_excerpt": "anthropic overloaded_error (529)"}), A)
    assert_eq("session limit result_text", S({"state": "inconclusive_no_final"}, {"result_text": "You have reached your session limit"}), A)
    assert_eq("cursor settings block", S({"state": "failed"}, {"result_text": "Check your settings to continue"}), A)
    assert_eq("review-job stderr excerpt in record.error", S({"state": "failed", "error": {"stderr_excerpt": "provider blocked: Check your settings to continue"}}, None), A)
    # ledger record.error is a signal carrier even with no status file
    assert_eq("record.error carrier", S({"state": "failed", "error": "resource_exhausted"}, None), A)
    assert_eq("worker_dead record.error carrier", S({"state": "worker_dead", "error": {"tail_excerpt": "usage limit"}}, None), A)
    # goal-flight's OWN capacity gate must never count (self-referential
    # pressure would let our queueing falsely halve provider caps)
    assert_eq("blocked_capacity guard", S({"state": "blocked_capacity"}, {"error": "machine_worker_cap rate limit"}), None)
    # auth-blocked and successful dispatches stay excluded even with limit text
    assert_eq("blocked_auth excluded", S({"state": "blocked_auth"}, {"error": "429"}), None)
    assert_eq("complete excluded", S({"state": "complete"}, {"result_text": "rate limit discussion in a review"}), None)
    # 2026-06-16 fold — codex retry-exhaustion + xAI credit-exhaustion (new modes
    # not already caught by the "rate limit"/"429" substrings)
    assert_eq("codex exceeded-retry", S({"state": "failed"}, {"error": "stream error: exceeded retry limit"}), A)
    assert_eq("xai credits depleted", S({"state": "failed"}, {"result_text": "Your API requests will be automatically rejected once your prepaid credits are depleted."}), A)
    assert_eq("xai payment_required", S({"state": "failed"}, {"error": "402 payment_required"}), A)
    # credit-exhaustion text echoed in a SUCCESSFUL run must not false-positive
    assert_eq("credits text complete excluded", S({"state": "complete"}, {"result_text": "note: prepaid credits are depleted soon"}), None)


def _run_tests():
    failed = []
    passed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            passed += 1
        except AssertionError as exc:
            failed.append((name, str(exc)))
        except Exception as exc:
            failed.append((name, f"{type(exc).__name__}: {exc}"))
    return passed, failed


if __name__ == "__main__":
    passed, failed = _run_tests()
    if failed:
        print(f"FAIL  tests/python/test_goalflight_rate_pressure.py ({len(failed)} failed of {passed + len(failed)})")
        for name, err in failed:
            print(f"  - {name}: {err}")
        sys.exit(1)
    print(f"PASS  tests/python/test_goalflight_rate_pressure.py ({passed} assertions)")
