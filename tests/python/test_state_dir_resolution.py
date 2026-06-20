"""Regression tests for shared goal-flight state-dir resolution.

Blank or whitespace-only GOALFLIGHT_STATE_DIR must fall back to the machine
default everywhere. A real non-blank value must still be honored at call time.
"""

from __future__ import annotations

import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_capacity as capacity  # noqa: E402
import goalflight_compat as compat  # noqa: E402
import goalflight_dispatch as dispatch  # noqa: E402
import goalflight_ledger as ledger  # noqa: E402
import goalflight_rate_pressure as rate_pressure  # noqa: E402


@contextmanager
def env_state_dir(value: str | None):
    sentinel = object()
    old = os.environ.get("GOALFLIGHT_STATE_DIR", sentinel)
    try:
        if value is None:
            os.environ.pop("GOALFLIGHT_STATE_DIR", None)
        else:
            os.environ["GOALFLIGHT_STATE_DIR"] = value
        yield
    finally:
        if old is sentinel:
            os.environ.pop("GOALFLIGHT_STATE_DIR", None)
        else:
            os.environ["GOALFLIGHT_STATE_DIR"] = str(old)


def assert_eq(name: str, got: object, expected: object) -> None:
    if got != expected:
        raise AssertionError(f"{name}: got {got!r}, expected {expected!r}")


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def resolver_paths() -> dict[str, Path]:
    return {
        "compat.resolve_state_dir": compat.resolve_state_dir(),
        "dispatch._state_dir": dispatch._state_dir(),
        "ledger.state_dir": ledger.state_dir(),
        "capacity.state_dir": capacity.state_dir(),
        "rate_pressure._default_state_dir": rate_pressure._default_state_dir(),
    }


def test_blank_env_falls_back_everywhere() -> None:
    expected = compat.default_state_dir()
    for poison in ("", " \t "):
        with env_state_dir(poison):
            for name, path in resolver_paths().items():
                assert_eq(f"{name} blank fallback", path, expected)
                assert_true(f"{name} must not resolve to Path('.')", path != Path("."))
                assert_true(
                    f"{name} must not resolve to cwd",
                    path.resolve() != Path.cwd().resolve(),
                )


def test_real_env_is_honored_at_call_time() -> None:
    with tempfile.TemporaryDirectory(prefix="goal-flight-state-dir-") as tmp:
        expected = Path(tmp) / "custom-state"
        with env_state_dir(str(expected)):
            for name, path in resolver_paths().items():
                assert_eq(f"{name} real env", path, expected)


def main() -> None:
    tests = [
        test_blank_env_falls_back_everywhere,
        test_real_env_is_honored_at_call_time,
    ]
    for test in tests:
        test()
    print(f"PASS tests/python/test_state_dir_resolution.py ({len(tests)} tests)")


if __name__ == "__main__":
    main()
