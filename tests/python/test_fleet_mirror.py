#!/usr/bin/env python3
"""Tests for fleet status mirror seq validation (Track A goal 9a)."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_fleet_mirror as mirror

FIXTURES = ROOT / "tests" / "fixtures" / "fleet_mirrors"


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


def test_dispatch_status_v1_fixture_normalized() -> None:
    # Field set copied from goalflight_dispatch.py's detached-launch status writer.
    payload = {
        "schema": "goalflight.status.v1",
        "dispatch_id": "gf-live-status-v1",
        "agent": "codex-acp",
        "worker_pid": 4242,
        "pgid": 4242,
        "worker_alive": True,
        "worker_identity": {
            "pid": 4242,
            "lstart": "Thu Jun 11 12:00:00 2026",
            "comm": "python3",
        },
        "expected_worker_identity": {
            "pid": 4242,
            "lstart": "Thu Jun 11 12:00:00 2026",
            "comm": "python3",
        },
        "tail_path": "/tmp/goal-flight/gf-live-status-v1.tail.log",
        "state": "starting",
        "reason": "watcher_launching",
        "updated_at": 1780000000,
    }
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "status.json"
        path.write_text(json.dumps(payload) + "\n")
        result = mirror.read_status_mirror(path)

    assert_true("ok", result.ok is True)
    assert_true("seq", result.last_seq == 178000000020)
    assert_true("payload", result.payload is not None)
    assert_true("normalized schema", result.payload["schema"] == mirror.STATUS_MIRROR_SCHEMA)
    assert_true("source schema", result.payload["source_schema"] == mirror.DISPATCH_STATUS_SCHEMA)
    assert_true("state", result.payload["state"] == "starting")
    assert_true("liveness", result.payload["liveness_state"] == "live")
    assert_true("identity", result.payload["worker_identity"]["pid"] == 4242)


def test_seq_regression_fails_closed() -> None:
    result = mirror.read_status_mirror(FIXTURES / "seq_regression.json", last_seq=3)
    assert_true("not ok", result.ok is False)
    assert_true("error code", result.error == mirror.ERROR_SEQ_REGRESSION)
    assert_true("payload preserved", result.payload is not None)


def test_epoch_change_allows_lower_seq() -> None:
    payload = {
        "schema": mirror.STATUS_MIRROR_SCHEMA,
        "epoch": "status-file-birth-2",
        "seq": 1,
        "dispatch_id": "acp-codex-fixture-epoch",
        "state": "running",
    }
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "status.json"
        path.write_text(json.dumps(payload) + "\n")
        result = mirror.read_status_mirror(path, last_seq=5, last_epoch="status-file-birth-1")

    assert_true("ok", result.ok is True)
    assert_true("last_seq reset", result.last_seq == 1)
    assert_true("epoch", result.epoch == "status-file-birth-2")


def test_same_epoch_seq_regression_fails_closed() -> None:
    payload = {
        "schema": mirror.STATUS_MIRROR_SCHEMA,
        "epoch": "status-file-birth-1",
        "seq": 1,
        "dispatch_id": "acp-codex-fixture-epoch",
        "state": "running",
    }
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "status.json"
        path.write_text(json.dumps(payload) + "\n")
        result = mirror.read_status_mirror(path, last_seq=5, last_epoch="status-file-birth-1")

    assert_true("not ok", result.ok is False)
    assert_true("error code", result.error == mirror.ERROR_SEQ_REGRESSION)
    assert_true("epoch preserved", result.epoch == "status-file-birth-1")


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


def test_legacy_record_without_epoch_uses_epoch_zero() -> None:
    result = mirror.read_status_mirror(FIXTURES / "valid_ok.json")
    assert_true("ok", result.ok is True)
    assert_true("epoch", result.epoch == mirror.LEGACY_EPOCH)
    assert_true("payload epoch", result.payload is not None and result.payload["epoch"] == mirror.LEGACY_EPOCH)


def main() -> None:
    test_valid_fixture_returns_payload_and_last_seq()
    test_dispatch_status_v1_fixture_normalized()
    test_seq_regression_fails_closed()
    test_epoch_change_allows_lower_seq()
    test_same_epoch_seq_regression_fails_closed()
    test_schema_mismatch()
    test_partial_json()
    test_missing_file()
    test_first_read_accepts_seq_zero()
    test_legacy_record_without_epoch_uses_epoch_zero()
    print("OK: fleet mirror tests pass")


if __name__ == "__main__":
    main()
