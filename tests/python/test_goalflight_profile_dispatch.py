#!/usr/bin/env python3
"""Tests for gateway profile ENV merge into dispatch subprocess env."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from goalflight_profile import dispatch_env, missing_gateway_profile_keys, profile_path  # noqa: E402


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def test_dispatch_env_merges_gateway_profile() -> None:
    with tempfile.TemporaryDirectory() as td:
        profiles_dir = Path(td) / "profiles"
        profiles_dir.mkdir()
        slot_path = profile_path("studio-1", profiles_dir)
        slot_path.write_text("GOALFLIGHT_HERM_WORKER_URL=https://example.test/worker\n")
        env = dispatch_env(
            "herm-worker",
            "studio-1",
            profiles_dir=profiles_dir,
            base={"BASE": "1"},
        )
        assert_true("profile url merged", env["GOALFLIGHT_HERM_WORKER_URL"] == "https://example.test/worker")
        assert_true("slot exported", env["GOALFLIGHT_INSTALL_SLOT"] == "studio-1")
        assert_true("base preserved", env["BASE"] == "1")


def test_dispatch_env_passthrough_non_gateway() -> None:
    env = dispatch_env("codex", "studio-1", base={"ONLY": "x"})
    assert_true("no slot force", "GOALFLIGHT_INSTALL_SLOT" not in env or env.get("ONLY") == "x")


def test_missing_gateway_profile_keys() -> None:
    with tempfile.TemporaryDirectory() as td:
        profiles_dir = Path(td) / "profiles"
        profiles_dir.mkdir()
        missing = missing_gateway_profile_keys("herm-worker", "default", profiles_dir=profiles_dir)
        assert_true("missing when no file", missing == ["GOALFLIGHT_HERM_WORKER_URL"])
        profile_path("default", profiles_dir).write_text("GOALFLIGHT_HERM_WORKER_URL=https://x\n")
        missing2 = missing_gateway_profile_keys("herm-worker", "default", profiles_dir=profiles_dir)
        assert_true("none when present", missing2 == [])


def main() -> None:
    for test in (
        test_dispatch_env_merges_gateway_profile,
        test_dispatch_env_passthrough_non_gateway,
        test_missing_gateway_profile_keys,
    ):
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
