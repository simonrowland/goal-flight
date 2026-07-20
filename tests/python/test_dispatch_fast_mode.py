"""--fast: urgent lane (skip queue) + per-engine premium processing tier.

--fast must (1) inject codex's `-c service_tier=priority` (OpenAI premium tier,
~1.5x spend) into the codex `exec` argv, (2) force --priority critical so the
dispatch skips the queue, (3) survive the detached replay argv, and (4) NOT inject
a tier for engines with no registered fast tier (queue-skip only). The tier map
(FAST_TIER_ARGV) is the extension point for other worker CLIs.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402

_TIER = "service_tier=priority"


def assert_true(name: str, cond: bool) -> None:
    if not cond:
        raise AssertionError(name)


def _codex_args(**over) -> argparse.Namespace:
    base = dict(agent="codex", read_only=False, model=None, cwd=None, fast=False)
    base.update(over)
    return argparse.Namespace(**base)


def test_fast_injects_service_tier_priority_for_codex() -> None:
    argv, _ = D.build_worker(_codex_args(fast=True), None, [])
    assert_true("service_tier=priority present", _TIER in argv)
    idx = argv.index(_TIER)
    assert_true("tier is a codex -c pair", argv[idx - 1] == "-c")


def test_no_fast_no_service_tier_for_codex() -> None:
    argv, _ = D.build_worker(_codex_args(fast=False), None, [])
    assert_true("no tier without --fast", _TIER not in argv)
    # and a codex arg Namespace lacking the attr entirely must not crash / inject
    argv2, _ = D.build_worker(argparse.Namespace(agent="codex", read_only=False, model=None, cwd=None), None, [])
    assert_true("no tier when fast attr absent", _TIER not in argv2)


def test_fast_tier_argv_empty_for_unsupported_engine() -> None:
    # grok-code has no registered fast tier -> queue-skip only, no tier argv
    assert_true("unsupported engine gets no tier", D._fast_tier_argv(argparse.Namespace(agent="grok-code", fast=True)) == [])
    # codex maps to the priority tier
    assert_true("codex maps to priority tier", D._fast_tier_argv(_codex_args(fast=True)) == ["-c", _TIER])
    # the registry is the documented extension point
    assert_true("codex registered in FAST_TIER_ARGV", "codex" in D.FAST_TIER_ARGV)


def test_apply_fast_mode_forces_critical() -> None:
    a = _codex_args(fast=True, priority="normal")
    D._apply_fast_mode(a)
    assert_true("fast forces critical", a.priority == "critical")
    # idempotent: applying again keeps critical
    D._apply_fast_mode(a)
    assert_true("idempotent", a.priority == "critical")
    # no --fast: priority untouched
    b = _codex_args(fast=False, priority="normal")
    D._apply_fast_mode(b)
    assert_true("no fast leaves priority", b.priority == "normal")
    # unsupported engine still gets the queue-skip
    g = argparse.Namespace(agent="grok-code", fast=True, priority="bulk")
    D._apply_fast_mode(g)
    assert_true("unsupported engine still skips queue", g.priority == "critical")


def test_acp_engine_gets_no_tier() -> None:
    # --shape acp remaps codex -> codex-acp (not in FAST_TIER_ARGV) and dispatches via
    # goalflight_acp_run.py, which never calls build_worker. The premium tier must NOT
    # be injected on that path (honest: queue-skip only). Guards the P1b fix.
    assert_true("codex-acp gets no tier", D._fast_tier_argv(argparse.Namespace(agent="codex-acp", fast=True)) == [])
    assert_true("grok-acp gets no tier", D._fast_tier_argv(argparse.Namespace(agent="grok-acp", fast=True)) == [])


def test_tier_argv_position_before_stdin_sentinel() -> None:
    argv, _ = D.build_worker(_codex_args(fast=True, model="gpt-5.6-sol"), None, [])
    assert_true("tier present", _TIER in argv)
    assert_true("tier is a -c pair", argv[argv.index(_TIER) - 1] == "-c")
    # the tier must land before the trailing '-' stdin sentinel and before --model,
    # never dangling past positional/tail args (a misplaced flag would be ignored by codex)
    assert_true("tier before stdin sentinel '-'", argv.index(_TIER) < argv.index("-"))
    assert_true("tier before --model", argv.index(_TIER) < argv.index("--model"))


def _replay_args(**over) -> argparse.Namespace:
    """Complete Namespace covering every field _canonical_replay_argv reads."""
    base = dict(
        agent="codex", dispatch_id="codex-1-1", cwd=None, shape="bash", priority="critical",
        billing="sub", poll_secs=2.0, max_idle_secs=600.0, prompt_file=None, prompt="hi",
        task_ids=[], model=None, os_sandbox=None, read_only=False, web_research_ok=False,
        web_qa=False,
        ignore_git_warn=False, no_orientation=False, capacity_wait_s=None, account=None,
        interactive=False, permission_mode=None, permission_dir=None,
        permission_inline_timeout_s=None, permission_user_timeout_s=None,
        controller_pid=None, fast=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_fast_propagates_through_replay_argv() -> None:
    on = D._canonical_replay_argv(_replay_args(fast=True), [], tail=Path("/tmp/t"), status_json=Path("/tmp/s"))
    assert_true("--fast survives detached replay", "--fast" in on)
    off = D._canonical_replay_argv(_replay_args(fast=False), [], tail=Path("/tmp/t"), status_json=Path("/tmp/s"))
    assert_true("no --fast when off", "--fast" not in off)


def main() -> None:
    tests = [
        test_fast_injects_service_tier_priority_for_codex,
        test_no_fast_no_service_tier_for_codex,
        test_fast_tier_argv_empty_for_unsupported_engine,
        test_apply_fast_mode_forces_critical,
        test_acp_engine_gets_no_tier,
        test_tier_argv_position_before_stdin_sentinel,
        test_fast_propagates_through_replay_argv,
    ]
    for t in tests:
        t()
    print(f"PASS tests/python/test_dispatch_fast_mode.py ({len(tests)} tests)")


if __name__ == "__main__":
    main()
