#!/usr/bin/env python3
"""Tests for fleet schema validators and bootstrap layout."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_fleet as fleet
import goalflight_fleet_schemas as schemas


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def test_valid_defaults_round_trip() -> None:
    for doc in (
        schemas.default_fleet_doc("host:1"),
        schemas.default_billing_doc(),
        schemas.default_steering_doc(),
    ):
        schemas.validate_document(doc)


def test_unknown_schema_version_rejected() -> None:
    bad = schemas.default_fleet_doc("host:1")
    bad["schema_version"] = 99
    try:
        schemas.validate_document(bad)
        assert_true("should reject unknown version", False)
    except schemas.SchemaError:
        pass


def test_bootstrap_layout() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        result = fleet.bootstrap(fleet_dir)
        assert_true("fleet.json created", (fleet_dir / "fleet.json").exists())
        assert_true("register dir", (fleet_dir / "register").is_dir())
        assert_true("aggregate created", (fleet_dir / "register" / "aggregate.json").exists())
        assert_true("validate passes", fleet.cmd_validate(
            type("Args", (), {"fleet_dir": fleet_dir})()
        ) == 0)
        assert_true("bootstrap idempotent", result["fleet.json"] in {"created", "exists"})


def test_billing_limit_pool_defaults() -> None:
    doc = schemas.default_billing_doc()
    schemas.validate_document(doc)
    assert_true("sample accounts", len(doc["accounts"]) >= 2)
    assert_true("limit_pool_id present", all("limit_pool_id" in a for a in doc["accounts"]))


def test_validate_file_fixture() -> None:
    fixture = ROOT / "tests" / "fixtures" / "fleet" / "fleet.json"
    if fixture.exists():
        assert_true("fixture valid", schemas.validate_file(fixture) == [])


def main() -> None:
    for test in (
        test_valid_defaults_round_trip,
        test_unknown_schema_version_rejected,
        test_bootstrap_layout,
        test_billing_limit_pool_defaults,
        test_validate_file_fixture,
    ):
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
