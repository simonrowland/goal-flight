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
ACP_FINAL_FAILURE_STATES = (
    "tool_timeout",
    "stalled",
    "remote_turn_silence",
    "failed_worktree",
)


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


def test_dispatch_status_v1_acp_final_failure_states_are_terminal() -> None:
    for index, state in enumerate(ACP_FINAL_FAILURE_STATES, start=1):
        payload = {
            "schema": "goalflight.status.v1",
            "dispatch_id": f"gf-final-failure-{index}",
            "state": state,
            "worker_alive": False,
            "updated_at": 1780000000 + index,
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "status.json"
            path.write_text(json.dumps(payload) + "\n")
            result = mirror.read_status_mirror(path)

        assert_true(f"{state} ok", result.ok is True)
        assert_true(f"{state} payload", result.payload is not None)
        assert_true(f"{state} state", result.payload["state"] == state)
        assert_true(f"{state} liveness", result.payload["liveness_state"] == "terminal")
        assert_true(f"{state} terminal", result.payload["terminal_state"] == "error")


def test_events_seen_seq_advances_on_state_transition() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "status.json"
        starting = {
            "schema": mirror.STATUS_MIRROR_SCHEMA,
            "epoch": "same-status-file",
            "events_seen": 0,
            "dispatch_id": "acp-codex-state-transition",
            "state": "starting",
            "updated_at": 1780000000,
        }
        running = dict(starting, state="running", updated_at=1780000001)

        path.write_text(json.dumps(starting) + "\n")
        first = mirror.read_status_mirror(path)
        path.write_text(json.dumps(running) + "\n")
        second = mirror.read_status_mirror(path, last_seq=first.last_seq, last_epoch=first.epoch)

    assert_true("first ok", first.ok is True)
    assert_true("first seq", first.last_seq == 20)
    assert_true("running ingested", second.ok is True)
    assert_true("running seq", second.last_seq == 30)


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


def test_legacy_epoch_zero_lineage_reset_resumes_when_worker_pid_changes() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "status.json"
        baseline = {
            "schema": mirror.STATUS_MIRROR_SCHEMA,
            "seq": 1500,
            "dispatch_id": "acp-codex-legacy-reset",
            "state": "running",
            "worker_pid": 1111,
            "worker_identity": {
                "pid": 1111,
                "lstart": "Thu Jun 11 12:00:00 2026",
                "comm": "python3",
            },
            "updated_at": 1780000000,
        }
        reset = {
            "schema": mirror.STATUS_MIRROR_SCHEMA,
            "events_seen": 0,
            "dispatch_id": "acp-codex-legacy-reset",
            "state": "running",
            "worker_pid": 2222,
            "worker_identity": {
                "pid": 2222,
                "lstart": "Thu Jun 11 12:01:00 2026",
                "comm": "python3",
            },
            "updated_at": 1780000060,
        }

        path.write_text(json.dumps(baseline) + "\n")
        first = mirror.read_status_mirror(path)
        path.write_text(json.dumps(reset) + "\n")
        resumed = mirror.read_status_mirror(
            path,
            last_seq=first.last_seq,
            last_epoch=first.epoch,
            last_lineage_identity=first.lineage_identity,
        )

    assert_true("baseline ok", first.ok is True)
    assert_true("legacy epoch", first.epoch == mirror.LEGACY_EPOCH)
    assert_true("baseline identity", first.lineage_identity == {"worker_pid": 1111, "worker_start_time": "Thu Jun 11 12:00:00 2026"})
    assert_true("reset accepted", resumed.ok is True)
    assert_true("baseline reset", resumed.last_seq == 30)
    assert_true("reset detail", resumed.detail is not None and "lineage reset accepted" in resumed.detail)
    assert_true("reset identity", resumed.lineage_identity == {"worker_pid": 2222, "worker_start_time": "Thu Jun 11 12:01:00 2026"})


def test_legacy_epoch_zero_stale_replay_still_rejected() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "status.json"
        baseline = {
            "schema": mirror.STATUS_MIRROR_SCHEMA,
            "seq": 2030,
            "dispatch_id": "acp-codex-legacy-stale",
            "state": "running",
            "worker_pid": 4242,
            "updated_at": 1780000000,
        }
        stale = {
            "schema": mirror.STATUS_MIRROR_SCHEMA,
            "events_seen": 5,
            "dispatch_id": "acp-codex-legacy-stale",
            "state": "running",
            "worker_pid": 4242,
            "updated_at": 1780000060,
        }

        path.write_text(json.dumps(baseline) + "\n")
        first = mirror.read_status_mirror(path)
        path.write_text(json.dumps(stale) + "\n")
        rejected = mirror.read_status_mirror(
            path,
            last_seq=first.last_seq,
            last_epoch=first.epoch,
            last_lineage_identity=first.lineage_identity,
        )

    assert_true("baseline ok", first.ok is True)
    assert_true("stale rejected", rejected.ok is False)
    assert_true("error code", rejected.error == mirror.ERROR_SEQ_REGRESSION)
    assert_true("lower replay seq", rejected.last_seq == 530)


def test_legacy_epoch_zero_without_birth_signal_rejects_lower_seq() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "status.json"
        baseline = {
            "schema": mirror.STATUS_MIRROR_SCHEMA,
            "seq": 1500,
            "dispatch_id": "acp-codex-legacy-no-birth",
            "state": "running",
            "worker_pid": 1111,
            "updated_at": 1780000000,
        }
        ambiguous_reset = {
            "schema": mirror.STATUS_MIRROR_SCHEMA,
            "events_seen": 0,
            "dispatch_id": "acp-codex-legacy-no-birth",
            "state": "running",
            "updated_at": 1780000060,
        }

        path.write_text(json.dumps(baseline) + "\n")
        first = mirror.read_status_mirror(path)
        path.write_text(json.dumps(ambiguous_reset) + "\n")
        rejected = mirror.read_status_mirror(
            path,
            last_seq=first.last_seq,
            last_epoch=first.epoch,
            last_lineage_identity=first.lineage_identity,
        )

    assert_true("baseline ok", first.ok is True)
    assert_true("ambiguous reset rejected", rejected.ok is False)
    assert_true("error code", rejected.error == mirror.ERROR_SEQ_REGRESSION)


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
    test_dispatch_status_v1_acp_final_failure_states_are_terminal()
    test_events_seen_seq_advances_on_state_transition()
    test_seq_regression_fails_closed()
    test_epoch_change_allows_lower_seq()
    test_same_epoch_seq_regression_fails_closed()
    test_legacy_epoch_zero_lineage_reset_resumes_when_worker_pid_changes()
    test_legacy_epoch_zero_stale_replay_still_rejected()
    test_legacy_epoch_zero_without_birth_signal_rejects_lower_seq()
    test_schema_mismatch()
    test_partial_json()
    test_missing_file()
    test_first_read_accepts_seq_zero()
    test_legacy_record_without_epoch_uses_epoch_zero()
    print("OK: 15 fleet mirror tests pass")


if __name__ == "__main__":
    main()
