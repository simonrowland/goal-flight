#!/usr/bin/env python3
"""Tests for fleet status mirror seq validation (Track A goal 9a)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_fleet_mirror as mirror

FIXTURES = ROOT / "test" / "fixtures" / "fleet_mirrors"


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def test_valid_fixture_returns_payload_and_last_seq() -> None:
    result = mirror.read_status_mirror(FIXTURES / "valid_ok.json")
    assert_true("ok", result.ok is True)
    assert_true("no error", result.error is None)
    assert_true("last_seq", result.last_seq == 7)
    assert_true("payload", result.payload is not None)
    assert_true("dispatch_id", result.payload["dispatch_id"] == "acp-codex-fixture-01")


def test_seq_regression_fails_closed() -> None:
    result = mirror.read_status_mirror(FIXTURES / "seq_regression.json", last_seq=3)
    assert_true("not ok", result.ok is False)
    assert_true("error code", result.error == mirror.ERROR_SEQ_REGRESSION)
    assert_true("payload preserved", result.payload is not None)


def test_schema_mismatch() -> None:
    result = mirror.read_status_mirror(FIXTURES / "schema_mismatch.json")
    assert_true("not ok", result.ok is False)
    assert_true("error code", result.error == mirror.ERROR_SCHEMA_MISMATCH)


def test_partial_json() -> None:
    result = mirror.read_status_mirror(FIXTURES / "partial.json")
    assert_true("not ok", result.ok is False)
    assert_true("error code", result.error == mirror.ERROR_PARTIAL_JSON)


def test_missing_file() -> None:
    result = mirror.read_status_mirror(FIXTURES / "does-not-exist.json")
    assert_true("not ok", result.ok is False)
    assert_true("error code", result.error == mirror.ERROR_MISSING_FILE)


def test_first_read_accepts_seq_zero() -> None:
    path = FIXTURES / "valid_ok.json"
    payload = path.read_text().replace('"seq": 7', '"seq": 0')
    tmp = FIXTURES / "_tmp_seq_zero.json"
    tmp.write_text(payload)
    try:
        result = mirror.read_status_mirror(tmp)
        assert_true("ok", result.ok is True)
        assert_true("last_seq", result.last_seq == 0)
    finally:
        tmp.unlink(missing_ok=True)


def main() -> None:
    test_valid_fixture_returns_payload_and_last_seq()
    test_seq_regression_fails_closed()
    test_schema_mismatch()
    test_partial_json()
    test_missing_file()
    test_first_read_accepts_seq_zero()
    print("OK: fleet mirror tests pass")


if __name__ == "__main__":
    main()
