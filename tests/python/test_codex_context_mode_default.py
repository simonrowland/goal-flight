"""context-mode defaults OFF for dispatched codex workers (#18).

context-mode's ctx_index elicitation (request_user_input) is unsupported in
codex `exec` and wedges the worker. goal-flight therefore disables context-mode at
the codex worker boundary by default, opt back in with GOALFLIGHT_CODEX_CONTEXT_MODE.
Other acp engines (grok-acp, cursor) keep context-mode enabled.
"""

from __future__ import annotations

import argparse
import os
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402

_DISABLE = "mcp_servers.context-mode.enabled=false"


def assert_true(name: str, cond: bool) -> None:
    if not cond:
        raise AssertionError(name)


@contextmanager
def env(var: str, value: str | None):
    sentinel = object()
    old = os.environ.get(var, sentinel)
    try:
        if value is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = value
        yield
    finally:
        if old is sentinel:
            os.environ.pop(var, None)
        else:
            os.environ[var] = str(old)


def _codex_args() -> argparse.Namespace:
    return argparse.Namespace(agent="codex", read_only=False, model=None, cwd=None)


def test_codex_exec_disables_context_mode_by_default() -> None:
    with env("GOALFLIGHT_CODEX_CONTEXT_MODE", None):
        argv, _ = D.build_worker(_codex_args(), None, [])
    assert_true("disable override present by default", _DISABLE in argv)
    # poison: it must be a codex -c override pair
    idx = argv.index(_DISABLE)
    assert_true("override is a -c pair", argv[idx - 1] == "-c")


def test_codex_exec_context_mode_opt_in() -> None:
    for on in ("1", "true", "enabled", "on"):
        with env("GOALFLIGHT_CODEX_CONTEXT_MODE", on):
            argv, _ = D.build_worker(_codex_args(), None, [])
        assert_true(f"opt-in {on}: no disable override", _DISABLE not in argv)


def test_acp_default_off_for_codex_only() -> None:
    with env("GOALFLIGHT_CODEX_CONTEXT_MODE", None):
        assert_true(
            "codex-acp default disabled",
            D._acp_context_mode_default(argparse.Namespace(agent="codex-acp")) == "disabled",
        )
        assert_true(
            "grok-acp default enabled",
            D._acp_context_mode_default(argparse.Namespace(agent="grok-acp")) == "enabled",
        )
        assert_true(
            "cursor default enabled",
            D._acp_context_mode_default(argparse.Namespace(agent="cursor")) == "enabled",
        )
    with env("GOALFLIGHT_CODEX_CONTEXT_MODE", "enabled"):
        assert_true(
            "codex-acp opt-in enabled",
            D._acp_context_mode_default(argparse.Namespace(agent="codex-acp")) == "enabled",
        )


def main() -> None:
    tests = [
        test_codex_exec_disables_context_mode_by_default,
        test_codex_exec_context_mode_opt_in,
        test_acp_default_off_for_codex_only,
    ]
    for t in tests:
        t()
    print(f"PASS tests/python/test_codex_context_mode_default.py ({len(tests)} tests)")


if __name__ == "__main__":
    main()
