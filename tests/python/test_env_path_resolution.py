"""Regression tests for blank-safe path-valued env resolvers."""

from __future__ import annotations

import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_compat as compat  # noqa: E402
import goalflight_controller_preflight as controller_preflight  # noqa: E402
import goalflight_doctor as doctor  # noqa: E402
import goalflight_fleet as fleet  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_rate_pressure as rate_pressure  # noqa: E402


@contextmanager
def env_var(name: str, value: str | None):
    sentinel = object()
    old = os.environ.get(name, sentinel)
    try:
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
        yield
    finally:
        if old is sentinel:
            os.environ.pop(name, None)
        else:
            os.environ[name] = str(old)


def assert_eq(name: str, got: object, expected: object) -> None:
    if got != expected:
        raise AssertionError(f"{name}: got {got!r}, expected {expected!r}")


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def doctor_wsl_fleet_dir() -> Path:
    result = doctor.check_wsl_filesystems(ROOT)
    for item in result["details"]:
        if item["label"] == "fleet_dir":
            return Path(item["path"])
    raise AssertionError("doctor.check_wsl_filesystems missing fleet_dir detail")


def doctor_wsl_fleet_dir_without_fleet_module() -> Path:
    old = doctor.goalflight_fleet
    try:
        doctor.goalflight_fleet = None
        return doctor_wsl_fleet_dir()
    finally:
        doctor.goalflight_fleet = old


def fleet_resolver_paths() -> dict[str, Path]:
    return {
        "compat.resolve_env_path": compat.resolve_env_path(
            "GOALFLIGHT_FLEET_DIR", Path.home() / ".goal-flight" / "fleet"
        ),
        "fleet.default_fleet_dir": fleet.default_fleet_dir(),
        "rate_pressure.default_fleet_dir": rate_pressure.default_fleet_dir(),
        "controller_preflight.default_fleet_dir": controller_preflight.default_fleet_dir(),
        "messages.default_fleet_dir": messages.default_fleet_dir(),
        "doctor.check_wsl_filesystems": doctor_wsl_fleet_dir(),
        "doctor.check_wsl_filesystems fallback": doctor_wsl_fleet_dir_without_fleet_module(),
    }


def test_blank_fleet_dir_falls_back_everywhere() -> None:
    expected = Path.home() / ".goal-flight" / "fleet"
    for poison in ("", " \t "):
        with env_var("GOALFLIGHT_FLEET_DIR", poison):
            for name, path in fleet_resolver_paths().items():
                assert_eq(f"{name} blank fallback", path, expected)
                assert_true(f"{name} must not resolve to Path('.')", path != Path("."))
                assert_true(
                    f"{name} must not resolve to cwd",
                    path.resolve() != Path.cwd().resolve(),
                )


def test_real_fleet_dir_is_honored_everywhere() -> None:
    with tempfile.TemporaryDirectory(prefix="goal-flight-fleet-dir-") as tmp:
        expected = Path(tmp) / "custom-fleet"
        with env_var("GOALFLIGHT_FLEET_DIR", str(expected)):
            for name, path in fleet_resolver_paths().items():
                assert_eq(f"{name} real env", path, expected)


def test_blank_messages_dir_falls_back() -> None:
    expected = Path.home() / ".goal-flight" / "messages"
    for poison in ("", " \t "):
        with env_var("GOALFLIGHT_MESSAGES_DIR", poison):
            path = messages.default_messages_dir()
            assert_eq("messages.default_messages_dir blank fallback", path, expected)
            assert_true(
                "messages.default_messages_dir must not resolve to cwd",
                path.resolve() != Path.cwd().resolve(),
            )


def test_real_messages_dir_is_honored() -> None:
    with tempfile.TemporaryDirectory(prefix="goal-flight-messages-dir-") as tmp:
        expected = Path(tmp) / "custom-messages"
        with env_var("GOALFLIGHT_MESSAGES_DIR", str(expected)):
            assert_eq("messages.default_messages_dir real env", messages.default_messages_dir(), expected)


def main() -> None:
    tests = [
        test_blank_fleet_dir_falls_back_everywhere,
        test_real_fleet_dir_is_honored_everywhere,
        test_blank_messages_dir_falls_back,
        test_real_messages_dir_is_honored,
    ]
    for test in tests:
        test()
    print(f"PASS tests/python/test_env_path_resolution.py ({len(tests)} tests)")


if __name__ == "__main__":
    main()
