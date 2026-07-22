"""--fast is an urgent lane only: it skips the queue without buying a premium tier.

The flag must force --priority critical for every engine/shape, remain idempotent,
survive detached replay, leave priority untouched when absent, and never add a
premium service configuration to the codex `exec` argv.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402


def assert_true(name: str, cond: bool) -> None:
    if not cond:
        raise AssertionError(name)


def _codex_args(**over) -> argparse.Namespace:
    base = dict(agent="codex", read_only=False, model=None, cwd=None, fast=False)
    base.update(over)
    return argparse.Namespace(**base)


def test_fast_codex_exec_has_no_premium_tier() -> None:
    argv, _ = D.build_worker(_codex_args(fast=True), None, [])
    tier_key = "service" + "_tier"
    priority_tier = tier_key + "=priority"
    assert_true("priority tier absent", priority_tier not in argv)
    assert_true("all service-tier tokens absent", all(tier_key not in token for token in argv))


def test_no_fast_codex_exec_has_no_premium_tier() -> None:
    tier_key = "service" + "_tier"
    argv, _ = D.build_worker(_codex_args(fast=False), None, [])
    assert_true("no tier without --fast", all(tier_key not in token for token in argv))
    # A codex Namespace lacking the attr entirely must not crash or inject a tier.
    argv2, _ = D.build_worker(argparse.Namespace(agent="codex", read_only=False, model=None, cwd=None), None, [])
    assert_true("no tier when fast attr absent", all(tier_key not in token for token in argv2))


def test_fast_forces_critical_for_codex() -> None:
    a = _codex_args(fast=True, priority="normal")
    D._apply_fast_mode(a)
    assert_true("codex fast forces critical", a.priority == "critical")


def test_fast_forces_critical_for_unsupported_engine() -> None:
    g = argparse.Namespace(agent="grok-code", fast=True, priority="bulk")
    D._apply_fast_mode(g)
    assert_true("unsupported engine still skips queue", g.priority == "critical")


def test_fast_mode_is_idempotent() -> None:
    a = _codex_args(fast=True, priority="normal")
    D._apply_fast_mode(a)
    D._apply_fast_mode(a)
    assert_true("idempotent", a.priority == "critical")


def test_no_fast_leaves_priority_untouched() -> None:
    b = _codex_args(fast=False, priority="normal")
    D._apply_fast_mode(b)
    assert_true("no fast leaves priority", b.priority == "normal")


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
        test_fast_codex_exec_has_no_premium_tier,
        test_no_fast_codex_exec_has_no_premium_tier,
        test_fast_forces_critical_for_codex,
        test_fast_forces_critical_for_unsupported_engine,
        test_fast_mode_is_idempotent,
        test_no_fast_leaves_priority_untouched,
        test_fast_propagates_through_replay_argv,
    ]
    for t in tests:
        t()
    print(f"PASS tests/python/test_dispatch_fast_mode.py ({len(tests)} tests)")


if __name__ == "__main__":
    main()
