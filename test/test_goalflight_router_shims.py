#!/usr/bin/env python3
"""Tests for action router dry-run default and gateway shims."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def test_route_defaults_to_dry_run() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "goalflight_actions.py"),
            "route",
            "core",
            "doctor",
            "read",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert_true("dry-run exit 0", proc.returncode == 0)
    assert_true("prints command", "goalflight_doctor.py" in proc.stdout)


def test_gateway_shim_probes() -> None:
    env = {**os.environ, "PATH": str(ROOT / "bin") + os.pathsep + os.environ.get("PATH", "")}
    for binary in ("gf-herm-worker", "gf-cla-worker", "gf-paperclip"):
        version = subprocess.run([binary, "--version"], env=env, capture_output=True, text=True, check=False)
        assert_true(f"{binary} --version", version.returncode == 0 and "gateway-stub" in version.stdout)
        help_out = subprocess.run([binary, "--help"], env=env, capture_output=True, text=True, check=False)
        assert_true(f"{binary} --help", help_out.returncode == 0)


def main() -> None:
    for test in (test_route_defaults_to_dry_run, test_gateway_shim_probes):
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
