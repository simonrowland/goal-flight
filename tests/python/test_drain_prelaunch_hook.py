"""Regression tests for the optional drain pre-launch hook (dispatch spine).

The hook is a neutral, agent-agnostic extension point: an operator may install a
local-only hook that runs once per drain pass just before workers launch. The
tracked spine must:
  - no-op silently when no hook is installed,
  - invoke the hook (fire-and-forget) with this pass's agent labels when present,
  - honor the GOALFLIGHT_DRAIN_PRELAUNCH_HOOK override,
  - never raise, even if the hook subprocess errors.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402


def assert_eq(name: str, got: object, expected: object) -> None:
    if got != expected:
        raise AssertionError(f"{name}: got {got!r}, expected {expected!r}")


def assert_true(name: str, cond: bool) -> None:
    if not cond:
        raise AssertionError(name)


def _entries(*scan_entries):
    # _drain_queue_once iterates 4-tuples: (sort_key, path, scan_entry, read_error)
    return [("k", Path("/x"), se, None) for se in scan_entries]


def test_pass_agent_labels_distinct_and_sorted() -> None:
    entries = _entries({"agent": "grok-code"}, {"agent": "codex"}, {"agent": "codex"}, None, {})
    assert_eq("distinct sorted labels", D._pass_agent_labels(entries), ["codex", "grok-code"])
    assert_eq("empty when no agents", D._pass_agent_labels(_entries(None, {})), [])


def test_hook_path_env_override() -> None:
    saved = os.environ.get("GOALFLIGHT_DRAIN_PRELAUNCH_HOOK")
    try:
        os.environ["GOALFLIGHT_DRAIN_PRELAUNCH_HOOK"] = "/tmp/custom-hook"
        assert_eq("env override", D._drain_prelaunch_hook_path(), Path("/tmp/custom-hook"))
        os.environ.pop("GOALFLIGHT_DRAIN_PRELAUNCH_HOOK")
        assert_true("default under ext", D._drain_prelaunch_hook_path().name == "drain-prelaunch-hook")
    finally:
        if saved is None:
            os.environ.pop("GOALFLIGHT_DRAIN_PRELAUNCH_HOOK", None)
        else:
            os.environ["GOALFLIGHT_DRAIN_PRELAUNCH_HOOK"] = saved


def _patch(monkey: dict):
    saved = {k: getattr(D, k) for k in monkey}
    for k, v in monkey.items():
        setattr(D, k, v)
    return lambda: [setattr(D, k, saved[k]) for k in saved]


def test_hook_noop_when_absent() -> None:
    calls: list = []

    class _Rec:
        def run(self, *a, **k):
            calls.append(a)

    missing = Path(tempfile.gettempdir()) / "no-such-drain-prelaunch-hook"
    restore = _patch({"_drain_prelaunch_hook_path": lambda: missing, "subprocess": _Rec()})
    try:
        D._run_drain_prelaunch_hook(["codex"])
        assert_eq("no subprocess when absent", calls, [])
    finally:
        restore()


def test_hook_invokes_with_agent_labels_when_present() -> None:
    calls: list = []

    class _Rec:
        def run(self, argv, **k):
            calls.append((argv, k))

    with tempfile.TemporaryDirectory() as d:
        hook = Path(d) / "drain-prelaunch-hook"
        hook.write_text("#!/bin/sh\n", encoding="utf-8")
        restore = _patch({"_drain_prelaunch_hook_path": lambda: hook, "subprocess": _Rec()})
        try:
            D._run_drain_prelaunch_hook(["codex", "grok-code"])
            assert_eq("one call", len(calls), 1)
            argv = calls[0][0]
            assert_eq("hook path first", argv[0], str(hook))
            assert_true("passes agent labels", "codex" in argv and "grok-code" in argv)
            assert_true("time-bounded", calls[0][1].get("timeout") is not None)
        finally:
            restore()


def test_hook_swallows_errors() -> None:
    class _Boom:
        def run(self, *a, **k):
            raise OSError("boom")

    with tempfile.TemporaryDirectory() as d:
        hook = Path(d) / "drain-prelaunch-hook"
        hook.write_text("#!/bin/sh\n", encoding="utf-8")
        restore = _patch({"_drain_prelaunch_hook_path": lambda: hook, "subprocess": _Boom()})
        try:
            D._run_drain_prelaunch_hook(["codex"])  # must NOT raise
        finally:
            restore()


def main() -> None:
    tests = [
        test_pass_agent_labels_distinct_and_sorted,
        test_hook_path_env_override,
        test_hook_noop_when_absent,
        test_hook_invokes_with_agent_labels_when_present,
        test_hook_swallows_errors,
    ]
    for t in tests:
        t()
    print(f"PASS tests/python/test_drain_prelaunch_hook.py ({len(tests)} tests)")


if __name__ == "__main__":
    main()
