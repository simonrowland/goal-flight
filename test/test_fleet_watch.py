#!/usr/bin/env python3
"""Hermetic tests for fleet watch mirror ingest (Track A goal 10a)."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_fleet as fleet
import goalflight_fleet_mirror as mirror
import goalflight_fleet_watch as fleet_watch

FIXTURES = ROOT / "test" / "fixtures" / "fleet_mirrors"


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def _status_payload(*, dispatch_id: str, seq: int, state: str = "running") -> str:
    return json.dumps(
        {
            "schema": mirror.STATUS_MIRROR_SCHEMA,
            "seq": seq,
            "dispatch_id": dispatch_id,
            "state": state,
            "updated_at": "2026-05-24T12:00:00+00:00",
        }
    )


class FakeTransport:
    def __init__(self, by_dispatch: dict[str, str | fleet_watch.RemoteFetchResult]) -> None:
        self._by_dispatch = by_dispatch
        self.calls: list[tuple[str, str]] = []

    def fetch_remote_status(
        self,
        *,
        node_id: str,
        dispatch_id: str,
        remote_status_path: str,
        node_entry: dict | None,
    ) -> fleet_watch.RemoteFetchResult:
        self.calls.append((dispatch_id, remote_status_path))
        payload = self._by_dispatch.get(dispatch_id)
        if payload is None:
            return fleet_watch.RemoteFetchResult(ok=False, error="not configured in fake transport")
        if isinstance(payload, fleet_watch.RemoteFetchResult):
            return payload
        return fleet_watch.RemoteFetchResult(ok=True, content=payload)


def _fixture_fleet(fleet_dir: Path) -> None:
    fleet.bootstrap(fleet_dir)
    fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
    fleet_doc["nodes"] = {
        "build-1": {
            "node_id": "build-1",
            "status": "active",
            "ssh": {"alias": "build-1", "hostname": "build-1.example"},
            "repo_root": "/srv/goal-flight",
            "state_dir": "/home/dev/.goal-flight",
            "billing_accounts": [],
            "added_at": "2026-05-24T12:00:00+00:00",
        }
    }
    fleet._atomic_write_json(fleet_dir / "fleet.json", fleet_doc)


def _write_dispatch(
    fleet_dir: Path,
    *,
    dispatch_id: str = "acp-codex-watch-01",
    seq: int = 3,
) -> Path:
    dispatch_dir = fleet_dir / "register" / "dispatches" / dispatch_id
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    status_path = dispatch_dir / "status.json"
    status_path.write_text(_status_payload(dispatch_id=dispatch_id, seq=seq) + "\n")
    fleet._atomic_write_json(
        dispatch_dir / "meta.json",
        {
            "dispatch_id": dispatch_id,
            "node_id": "build-1",
            "remote_status_path": "/remote/runs/status.json",
            "lease_active": True,
            "last_mirror_seq": seq,
        },
    )
    fleet._atomic_write_json(
        fleet_dir / "register" / "aggregate.json",
        {
            "schema": "goalflight.fleet.register.aggregate.v1",
            "schema_version": 1,
            "min_reader_version": 1,
            "open_user_needs": [],
            "active_dispatches": [dispatch_id],
            "last_steering": None,
        },
    )
    return status_path


def test_ingest_advances_mirror_and_meta() -> None:
    dispatch_id = "acp-codex-watch-01"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        status_path = _write_dispatch(fleet_dir, dispatch_id=dispatch_id, seq=3)
        transport = FakeTransport({dispatch_id: _status_payload(dispatch_id=dispatch_id, seq=4)})

        row = fleet_watch.ingest_dispatch_mirror(
            fleet_dir,
            dispatch_id,
            fleet.read_json(status_path.parent / "meta.json"),
            transport,
            node_entry=fleet.read_json(fleet_dir / "fleet.json")["nodes"]["build-1"],
        )

        assert_true("ingested", row.ok and row.action == "ingested" and row.seq == 4)
        mirror_result = mirror.read_status_mirror(status_path)
        assert_true("mirror seq", mirror_result.ok and mirror_result.last_seq == 4)
        meta = fleet.read_json(status_path.parent / "meta.json")
        assert_true("meta seq", meta.get("last_mirror_seq") == 4)
        assert_true("meta not stale", meta.get("mirror_stale") is False)


def test_seq_regression_preserves_last_good_mirror() -> None:
    dispatch_id = "acp-codex-watch-02"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        status_path = _write_dispatch(fleet_dir, dispatch_id=dispatch_id, seq=5)
        before = status_path.read_text()
        transport = FakeTransport({dispatch_id: _status_payload(dispatch_id=dispatch_id, seq=3)})

        row = fleet_watch.ingest_dispatch_mirror(
            fleet_dir,
            dispatch_id,
            fleet.read_json(status_path.parent / "meta.json"),
            transport,
            node_entry=fleet.read_json(fleet_dir / "fleet.json")["nodes"]["build-1"],
        )

        assert_true("seq regression action", row.action == "seq_regression")
        assert_true("mirror unchanged", status_path.read_text() == before)
        meta = fleet.read_json(status_path.parent / "meta.json")
        assert_true("meta stale", meta.get("mirror_stale") is True)
        assert_true("meta error", meta.get("mirror_error") == mirror.ERROR_SEQ_REGRESSION)
        assert_true("row unknown", meta.get("row_state") == "unknown")
        assert_true("ingest stopped", meta.get("mirror_ingest_stopped") is True)


def test_seq_regression_stops_subsequent_ingest() -> None:
    dispatch_id = "acp-codex-watch-03"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        status_path = _write_dispatch(fleet_dir, dispatch_id=dispatch_id, seq=2)
        meta_path = status_path.parent / "meta.json"
        transport = FakeTransport({dispatch_id: _status_payload(dispatch_id=dispatch_id, seq=1)})

        first = fleet_watch.ingest_dispatch_mirror(
            fleet_dir,
            dispatch_id,
            fleet.read_json(meta_path),
            transport,
            node_entry=fleet.read_json(fleet_dir / "fleet.json")["nodes"]["build-1"],
        )
        assert_true("first regression", first.action == "seq_regression")

        transport._by_dispatch[dispatch_id] = _status_payload(dispatch_id=dispatch_id, seq=3)
        second = fleet_watch.ingest_dispatch_mirror(
            fleet_dir,
            dispatch_id,
            fleet.read_json(meta_path),
            transport,
            node_entry=fleet.read_json(fleet_dir / "fleet.json")["nodes"]["build-1"],
        )
        assert_true("skipped after stop", second.action == "skipped")


def test_validation_failure_does_not_truncate_mirror() -> None:
    dispatch_id = "acp-codex-watch-04"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        status_path = _write_dispatch(fleet_dir, dispatch_id=dispatch_id, seq=6)
        before = status_path.read_text()
        transport = FakeTransport({dispatch_id: FIXTURES.joinpath("partial.json").read_text()})

        row = fleet_watch.ingest_dispatch_mirror(
            fleet_dir,
            dispatch_id,
            fleet.read_json(status_path.parent / "meta.json"),
            transport,
            node_entry=fleet.read_json(fleet_dir / "fleet.json")["nodes"]["build-1"],
        )

        assert_true("validation failed", row.action == "validation_failed")
        assert_true("mirror preserved", status_path.read_text() == before)


def test_sync_fleet_mirrors_batch() -> None:
    dispatch_id = "acp-codex-watch-05"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        status_path = _write_dispatch(fleet_dir, dispatch_id=dispatch_id, seq=1)
        transport = FakeTransport({dispatch_id: _status_payload(dispatch_id=dispatch_id, seq=2)})

        result = fleet_watch.sync_fleet_mirrors(fleet_dir, transport)
        assert_true("one row", len(result.dispatches) == 1)
        assert_true("ingested batch", result.dispatches[0].action == "ingested")
        mirror_result = mirror.read_status_mirror(status_path)
        assert_true("batch seq", mirror_result.last_seq == 2)


def main() -> None:
    test_ingest_advances_mirror_and_meta()
    test_seq_regression_preserves_last_good_mirror()
    test_seq_regression_stops_subsequent_ingest()
    test_validation_failure_does_not_truncate_mirror()
    test_sync_fleet_mirrors_batch()
    print("OK: fleet watch tests pass")


if __name__ == "__main__":
    main()
