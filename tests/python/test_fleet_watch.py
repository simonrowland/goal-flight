#!/usr/bin/env python3
"""Hermetic tests for fleet watch mirror ingest (Track A goal 10a)."""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_fleet as fleet
import goalflight_fleet_mirror as mirror
import goalflight_fleet_reconcile as fleet_reconcile
import goalflight_fleet_watch as fleet_watch
import goalflight_liveness

FIXTURES = ROOT / "tests" / "fixtures" / "fleet_mirrors"


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


BASE_SHA = "0123456789abcdef0123456789abcdef01234567"


def _status_payload(
    *,
    dispatch_id: str,
    seq: int,
    state: str = "running",
    worker_pid: int | None = None,
    epoch: object | None = None,
) -> str:
    payload = {
        "schema": mirror.STATUS_MIRROR_SCHEMA,
        "seq": seq,
        "dispatch_id": dispatch_id,
        "state": state,
        "updated_at": "2026-05-24T12:00:00+00:00",
    }
    if epoch is not None:
        payload["epoch"] = epoch
    if worker_pid is not None:
        payload["worker_pid"] = worker_pid
        payload["worker_identity"] = {
            "pid": worker_pid,
            "lstart": "Thu Jun 11 12:00:00 2026",
            "comm": "python3",
        }
    return json.dumps(payload)


def _dispatch_status_v1_starting_payload(
    *,
    dispatch_id: str,
    updated_at: int = 1780000000,
    worker_pid: int = 5555,
    epoch: object | None = None,
) -> str:
    # Field set copied from goalflight_dispatch.py's detached-launch status writer.
    identity = {
        "pid": worker_pid,
        "lstart": "Thu Jun 11 12:00:00 2026",
        "comm": "python3",
    }
    payload = {
        "schema": "goalflight.status.v1",
        "dispatch_id": dispatch_id,
        "agent": "codex-acp",
        "worker_pid": worker_pid,
        "pgid": worker_pid,
        "worker_alive": True,
        "worker_identity": identity,
        "expected_worker_identity": identity,
        "tail_path": f"/tmp/goal-flight/{dispatch_id}.tail.log",
        "state": "starting",
        "reason": "watcher_launching",
        "updated_at": updated_at,
    }
    if epoch is not None:
        payload["epoch"] = epoch
    return json.dumps(payload)


def _write_real_status_v1(path: Path, *, dispatch_id: str, seq: int, state: str = "running") -> None:
    goalflight_liveness.write_status(
        path,
        {
            "schema": "goalflight.status.v1",
            "seq": seq,
            "dispatch_id": dispatch_id,
            "agent": "codex-acp",
            "worker_pid": 5555,
            "pgid": 5555,
            "worker_alive": True,
            "tail_path": f"/tmp/goal-flight/{dispatch_id}.tail.log",
            "state": state,
            "updated_at": 1780000000 + seq,
        },
    )


class FakeTransport:
    def __init__(
        self,
        by_dispatch: dict[
            str,
            str
            | fleet_watch.RemoteFetchResult
            | list[str | fleet_watch.RemoteFetchResult],
        ],
        identity_by_dispatch: dict[str, fleet_watch.RemoteIdentityResult] | None = None,
        porcelain_by_worktree: dict[str, fleet_watch.WorktreePorcelainResult] | None = None,
    ) -> None:
        self._by_dispatch = by_dispatch
        self._identity_by_dispatch = identity_by_dispatch or {}
        self._porcelain_by_worktree = porcelain_by_worktree or {}
        self.calls: list[tuple[str, str]] = []
        self.identity_calls: list[str] = []
        self.porcelain_calls: list[str] = []

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
        if isinstance(payload, list):
            if len(payload) > 1:
                item = payload.pop(0)
            elif payload:
                item = payload[0]
            else:
                item = None
            payload = item
        if payload is None:
            return fleet_watch.RemoteFetchResult(ok=False, error="not configured in fake transport")
        if isinstance(payload, fleet_watch.RemoteFetchResult):
            return payload
        return fleet_watch.RemoteFetchResult(ok=True, content=payload)

    def check_remote_identity(
        self,
        *,
        node_id: str,
        node_entry: dict | None,
        receipt: dict,
    ) -> fleet_watch.RemoteIdentityResult:
        dispatch_id = str(receipt.get("dispatch_id") or "")
        self.identity_calls.append(dispatch_id)
        return self._identity_by_dispatch.get(
            dispatch_id,
            fleet_watch.RemoteIdentityResult(ok=True, alive=True, identity=receipt.get("remote_identity")),
        )

    def check_worktree_porcelain(
        self,
        *,
        node_id: str,
        node_entry: dict | None,
        worktree_path: str,
    ) -> fleet_watch.WorktreePorcelainResult:
        self.porcelain_calls.append(worktree_path)
        if worktree_path in self._porcelain_by_worktree:
            return self._porcelain_by_worktree[worktree_path]
        return fleet_watch.WorktreePorcelainResult(
            ok=True,
            dirty=False,
            porcelain=None,
            worktree_path=worktree_path,
        )


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
    epoch: object | None = None,
    worker_pid: int | None = None,
) -> Path:
    dispatch_dir = fleet_dir / "register" / "dispatches" / dispatch_id
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    status_path = dispatch_dir / "status.json"
    remote_status_path = f"/home/dev/.goal-flight/dispatches/{dispatch_id}/status.json"
    worktree_path = f"/home/dev/.goal-flight/worktrees/{dispatch_id}"
    status_path.write_text(
        _status_payload(dispatch_id=dispatch_id, seq=seq, epoch=epoch, worker_pid=worker_pid) + "\n"
    )
    fleet._atomic_write_json(
        dispatch_dir / "meta.json",
        {
            "dispatch_id": dispatch_id,
            "node_id": "build-1",
            "remote_status_path": remote_status_path,
            "worktree_path": worktree_path,
            "lease_active": True,
            "last_mirror_seq": seq,
            "pid_hint": "alive",
            "launch_receipt": {
                "schema": "goalflight.fleet.launch_receipt.v1",
                "dispatch_id": dispatch_id,
                "node_id": "build-1",
                "remote_pid": 4242,
                "remote_lstart": "Thu Jun 11 12:00:00 2026",
                "remote_identity": {
                    "pid": 4242,
                    "lstart": "Thu Jun 11 12:00:00 2026",
                    "comm": "python3",
                },
                "remote_status_path": remote_status_path,
            },
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


def _write_unconfirmed_dispatch(
    fleet_dir: Path,
    *,
    dispatch_id: str,
    billing_account: str = "openai/default",
    remote_pid: int | None = None,
    launch_issued_at: str | None = None,
) -> Path:
    dispatch_dir = fleet_dir / "register" / "dispatches" / dispatch_id
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "dispatch_id": dispatch_id,
        "node_id": "build-1",
        "billing_account": billing_account,
        "remote_status_path": "/remote/runs/status.json",
        "remote_state_dir": "/home/dev/.goal-flight",
        "lease_active": True,
        "pid_hint": "unknown",
        "launch_unconfirmed": True,
        "launch_unconfirmed_error": "ssh dropped after launch issuance",
        "base_sha": BASE_SHA,
        "worktree_base_sha": BASE_SHA,
    }
    if launch_issued_at is not None:
        meta["launch_issued_at"] = launch_issued_at
        meta["launch_unconfirmed_at"] = launch_issued_at
    if remote_pid is not None:
        identity = {
            "pid": remote_pid,
            "lstart": "Thu Jun 11 12:00:00 2026",
            "comm": "python3",
        }
        meta.update(
            {
                "remote_pid": remote_pid,
                "remote_pid_lstart": identity["lstart"],
                "remote_pid_identity": identity,
            }
        )
    fleet._atomic_write_json(
        dispatch_dir / "meta.json",
        meta,
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
    return dispatch_dir / "status.json"


def _iso_seconds_ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


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


def test_ingest_rejects_mismatched_dispatch_id_payload() -> None:
    dispatch_id = "acp-codex-watch-dispatch-id"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        status_path = _write_dispatch(fleet_dir, dispatch_id=dispatch_id, seq=3)
        before = status_path.read_text()
        transport = FakeTransport(
            {dispatch_id: _status_payload(dispatch_id="acp-other-dispatch", seq=4)}
        )

        row = fleet_watch.ingest_dispatch_mirror(
            fleet_dir,
            dispatch_id,
            fleet.read_json(status_path.parent / "meta.json"),
            transport,
            node_entry=fleet.read_json(fleet_dir / "fleet.json")["nodes"]["build-1"],
        )

        assert_true("rejected", not row.ok and row.action == "dispatch_id_mismatch")
        assert_true("mirror unchanged", status_path.read_text() == before)
        meta = fleet.read_json(status_path.parent / "meta.json")
        assert_true("meta mismatch", meta.get("mirror_error") == "dispatch_id_mismatch")


def test_ingest_accepts_legacy_payload_without_dispatch_id() -> None:
    dispatch_id = "acp-codex-watch-legacy-no-id"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        status_path = _write_dispatch(fleet_dir, dispatch_id=dispatch_id, seq=3)
        legacy_payload = json.loads(_status_payload(dispatch_id=dispatch_id, seq=4))
        legacy_payload.pop("dispatch_id")
        transport = FakeTransport({dispatch_id: json.dumps(legacy_payload)})

        row = fleet_watch.ingest_dispatch_mirror(
            fleet_dir,
            dispatch_id,
            fleet.read_json(status_path.parent / "meta.json"),
            transport,
            node_entry=fleet.read_json(fleet_dir / "fleet.json")["nodes"]["build-1"],
        )

        assert_true("ingested legacy", row.ok and row.action == "ingested")
        mirror_payload = json.loads(status_path.read_text())
        assert_true("dispatch id backfilled", mirror_payload.get("dispatch_id") == dispatch_id)


def test_single_poll_backfills_launch_unconfirmed_receipt() -> None:
    dispatch_id = "acp-watch-unconfirmed-recover"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        status_path = _write_unconfirmed_dispatch(fleet_dir, dispatch_id=dispatch_id)
        transport = FakeTransport(
            {dispatch_id: _status_payload(dispatch_id=dispatch_id, seq=1, worker_pid=5555)}
        )

        row = fleet_watch.ingest_dispatch_mirror(
            fleet_dir,
            dispatch_id,
            fleet.read_json(status_path.parent / "meta.json"),
            transport,
            node_entry=fleet.read_json(fleet_dir / "fleet.json")["nodes"]["build-1"],
        )

        assert_true("ingested", row.ok and row.action == "ingested")
        meta = fleet.read_json(status_path.parent / "meta.json")
        assert_true("unconfirmed cleared", meta.get("launch_unconfirmed") is False)
        assert_true("recovered", meta.get("launch_recovered") is True)
        assert_true("row receipted", meta.get("row_state") == "launch_receipted")
        assert_true("receipt pid", meta.get("launch_receipt", {}).get("remote_pid") == 5555)
        assert_true("receipt base", meta.get("launch_receipt", {}).get("worktree_base_sha") == BASE_SHA)


def test_ingest_accepts_real_dispatch_status_v1_payload() -> None:
    dispatch_id = "acp-watch-status-v1-live"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        status_path = _write_unconfirmed_dispatch(fleet_dir, dispatch_id=dispatch_id)
        transport = FakeTransport(
            {dispatch_id: _dispatch_status_v1_starting_payload(dispatch_id=dispatch_id, worker_pid=6666)}
        )

        row = fleet_watch.ingest_dispatch_mirror(
            fleet_dir,
            dispatch_id,
            fleet.read_json(status_path.parent / "meta.json"),
            transport,
            node_entry=fleet.read_json(fleet_dir / "fleet.json")["nodes"]["build-1"],
        )

        assert_true("ingested", row.ok and row.action == "ingested")
        assert_true("seq", row.seq == 178000000020)
        mirror_payload = json.loads(status_path.read_text())
        assert_true("normalized schema", mirror_payload.get("schema") == mirror.STATUS_MIRROR_SCHEMA)
        assert_true("source schema", mirror_payload.get("source_schema") == mirror.DISPATCH_STATUS_SCHEMA)
        assert_true("state", mirror_payload.get("state") == "starting")
        assert_true("liveness", mirror_payload.get("liveness_state") == "live")
        assert_true("identity", mirror_payload.get("worker_identity", {}).get("pid") == 6666)
        meta = fleet.read_json(status_path.parent / "meta.json")
        assert_true("unconfirmed cleared", meta.get("launch_unconfirmed") is False)
        assert_true("receipt pid", meta.get("launch_receipt", {}).get("remote_pid") == 6666)


def test_seq_regression_preserves_last_good_mirror() -> None:
    dispatch_id = "acp-codex-watch-02"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        status_path = _write_dispatch(fleet_dir, dispatch_id=dispatch_id, seq=5, epoch="run-1")
        before = status_path.read_text()
        transport = FakeTransport(
            {dispatch_id: _status_payload(dispatch_id=dispatch_id, seq=3, epoch="run-1")}
        )

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


def test_validate_remote_content_rejects_same_pid_legacy_replay_with_newer_timestamp() -> None:
    dispatch_id = "acp-codex-watch-legacy-same-pid"
    baseline = {
        "schema": mirror.STATUS_MIRROR_SCHEMA,
        "seq": 2030,
        "dispatch_id": dispatch_id,
        "state": "running",
        "worker_pid": 4242,
        "updated_at": 1780000000,
    }
    replay = {
        "schema": mirror.STATUS_MIRROR_SCHEMA,
        "events_seen": 5,
        "dispatch_id": dispatch_id,
        "state": "running",
        "worker_pid": 4242,
        "updated_at": 1780000060,
    }

    first = fleet_watch.validate_remote_content(json.dumps(baseline), last_seq=None)
    rejected = fleet_watch.validate_remote_content(
        json.dumps(replay),
        last_seq=first.last_seq,
        last_epoch=first.epoch,
        last_lineage_identity=first.lineage_identity,
    )

    assert_true("baseline ok", first.ok is True)
    assert_true("baseline identity", first.lineage_identity == {"worker_pid": 4242})
    assert_true("same pid replay rejected", rejected.ok is False)
    assert_true("error code", rejected.error == mirror.ERROR_SEQ_REGRESSION)
    assert_true("lower replay seq", rejected.last_seq == 530)


def test_validate_remote_content_accepts_legacy_reset_with_cold_cache_and_new_pid() -> None:
    dispatch_id = "acp-codex-watch-legacy-cold-cache"
    baseline = {
        "schema": mirror.STATUS_MIRROR_SCHEMA,
        "seq": 2030,
        "dispatch_id": dispatch_id,
        "state": "running",
        "worker_pid": 1111,
        "updated_at": 1780000000,
    }
    reset = {
        "schema": mirror.STATUS_MIRROR_SCHEMA,
        "events_seen": 0,
        "dispatch_id": dispatch_id,
        "state": "running",
        "worker_pid": 2222,
        "updated_at": 1780000060,
    }

    first = fleet_watch.validate_remote_content(json.dumps(baseline), last_seq=None)
    baseline_cache = getattr(mirror, "_BASELINES_BY_DISPATCH_ID", None)
    if isinstance(baseline_cache, dict):
        baseline_cache.clear()
    resumed = fleet_watch.validate_remote_content(
        json.dumps(reset),
        last_seq=first.last_seq,
        last_epoch=first.epoch,
        last_lineage_identity=first.lineage_identity,
    )

    assert_true("baseline ok", first.ok is True)
    assert_true("reset accepted", resumed.ok is True)
    assert_true("reset seq", resumed.last_seq == 30)
    assert_true("reset identity", resumed.lineage_identity == {"worker_pid": 2222})


def test_legacy_pid_change_with_lower_seq_resumes_ingest_after_stop() -> None:
    dispatch_id = "acp-codex-watch-legacy-pid-reset"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        status_path = _write_dispatch(fleet_dir, dispatch_id=dispatch_id, seq=2030, worker_pid=1111)
        meta_path = status_path.parent / "meta.json"
        meta = fleet.read_json(meta_path)
        meta["mirror_ingest_stopped"] = True
        fleet._atomic_write_json(meta_path, meta)
        reset = {
            "schema": mirror.STATUS_MIRROR_SCHEMA,
            "events_seen": 0,
            "dispatch_id": dispatch_id,
            "state": "running",
            "worker_pid": 2222,
            "updated_at": 1780000060,
        }
        transport = FakeTransport({dispatch_id: json.dumps(reset)})

        row = fleet_watch.ingest_dispatch_mirror(
            fleet_dir,
            dispatch_id,
            fleet.read_json(meta_path),
            transport,
            node_entry=fleet.read_json(fleet_dir / "fleet.json")["nodes"]["build-1"],
        )

        assert_true("ingested after pid reset", row.ok and row.action == "ingested")
        mirror_payload = json.loads(status_path.read_text())
        assert_true("lower seq accepted", mirror_payload.get("seq") == 30)
        assert_true("new pid written", mirror_payload.get("worker_pid") == 2222)
        meta = fleet.read_json(meta_path)
        assert_true("ingest stop cleared", meta.get("mirror_ingest_stopped") is False)
        assert_true("stale cleared", meta.get("mirror_stale") is False)
        assert_true("lineage meta", meta.get("last_mirror_lineage_identity") == {"worker_pid": 2222})


def test_epoch_change_with_lower_seq_resumes_ingest_after_stop() -> None:
    dispatch_id = "acp-codex-watch-03"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        status_path = _write_dispatch(fleet_dir, dispatch_id=dispatch_id, seq=2, epoch="run-1")
        meta_path = status_path.parent / "meta.json"
        transport = FakeTransport(
            {dispatch_id: _status_payload(dispatch_id=dispatch_id, seq=1, epoch="run-1")}
        )

        first = fleet_watch.ingest_dispatch_mirror(
            fleet_dir,
            dispatch_id,
            fleet.read_json(meta_path),
            transport,
            node_entry=fleet.read_json(fleet_dir / "fleet.json")["nodes"]["build-1"],
        )
        assert_true("first regression", first.action == "seq_regression")

        transport._by_dispatch[dispatch_id] = _status_payload(
            dispatch_id=dispatch_id,
            seq=1,
            epoch="run-2",
        )
        second = fleet_watch.ingest_dispatch_mirror(
            fleet_dir,
            dispatch_id,
            fleet.read_json(meta_path),
            transport,
            node_entry=fleet.read_json(fleet_dir / "fleet.json")["nodes"]["build-1"],
        )
        assert_true("ingested after epoch reset", second.ok and second.action == "ingested")
        mirror_payload = json.loads(status_path.read_text())
        assert_true("lower seq accepted", mirror_payload.get("seq") == 1)
        assert_true("new epoch written", mirror_payload.get("epoch") == "run-2")
        meta = fleet.read_json(meta_path)
        assert_true("ingest stop cleared", meta.get("mirror_ingest_stopped") is False)
        assert_true("stale cleared", meta.get("mirror_stale") is False)
        assert_true("epoch meta", meta.get("last_mirror_epoch") == "run-2")


def test_real_status_writer_epoch_reset_resumes_ingest_after_recreate() -> None:
    dispatch_id = "acp-codex-watch-real-producer-epoch"
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        fleet_dir = base / "fleet"
        remote_status = base / "remote-status.json"
        _fixture_fleet(fleet_dir)
        status_path = _write_dispatch(fleet_dir, dispatch_id=dispatch_id, seq=5)
        meta_path = status_path.parent / "meta.json"

        _write_real_status_v1(remote_status, dispatch_id=dispatch_id, seq=5)
        status_path.write_text(remote_status.read_text(encoding="utf-8"), encoding="utf-8")
        first_epoch = json.loads(status_path.read_text(encoding="utf-8"))["epoch"]

        _write_real_status_v1(remote_status, dispatch_id=dispatch_id, seq=3)
        same_epoch_payload = remote_status.read_text(encoding="utf-8")
        same_epoch = json.loads(same_epoch_payload)["epoch"]
        assert_true("same writer epoch stable", same_epoch == first_epoch)

        transport = FakeTransport({dispatch_id: same_epoch_payload})
        first = fleet_watch.ingest_dispatch_mirror(
            fleet_dir,
            dispatch_id,
            fleet.read_json(meta_path),
            transport,
            node_entry=fleet.read_json(fleet_dir / "fleet.json")["nodes"]["build-1"],
        )
        assert_true("same epoch lower seq rejected", first.action == "seq_regression")

        remote_status.unlink()
        _write_real_status_v1(remote_status, dispatch_id=dispatch_id, seq=1)
        recreated_payload = remote_status.read_text(encoding="utf-8")
        recreated_epoch = json.loads(recreated_payload)["epoch"]
        assert_true("recreated writer epoch changed", recreated_epoch != first_epoch)

        transport._by_dispatch[dispatch_id] = recreated_payload
        second = fleet_watch.ingest_dispatch_mirror(
            fleet_dir,
            dispatch_id,
            fleet.read_json(meta_path),
            transport,
            node_entry=fleet.read_json(fleet_dir / "fleet.json")["nodes"]["build-1"],
        )
        assert_true("recreated lower seq ingested", second.ok and second.action == "ingested")
        mirror_payload = json.loads(status_path.read_text(encoding="utf-8"))
        assert_true("lower seq accepted", mirror_payload.get("seq") == 1)
        assert_true("real epoch written", mirror_payload.get("epoch") == recreated_epoch)


def test_successful_direct_read_clears_mirror_ingest_stop() -> None:
    dispatch_id = "acp-codex-watch-03b"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        status_path = _write_dispatch(fleet_dir, dispatch_id=dispatch_id, seq=2, epoch="run-1")
        meta_path = status_path.parent / "meta.json"
        meta = fleet.read_json(meta_path)
        meta["mirror_stale"] = True
        meta["mirror_error"] = mirror.ERROR_SEQ_REGRESSION
        meta["mirror_ingest_stopped"] = True
        meta["row_state"] = "unknown"
        fleet._atomic_write_json(meta_path, meta)
        transport = FakeTransport(
            {dispatch_id: _status_payload(dispatch_id=dispatch_id, seq=3, epoch="run-1")}
        )

        row = fleet_watch.ingest_dispatch_mirror(
            fleet_dir,
            dispatch_id,
            fleet.read_json(meta_path),
            transport,
            node_entry=fleet.read_json(fleet_dir / "fleet.json")["nodes"]["build-1"],
        )

        assert_true("direct fetch attempted", len(transport.calls) == 1)
        assert_true("direct read ingested", row.ok and row.action == "ingested" and row.seq == 3)
        meta = fleet.read_json(meta_path)
        assert_true("ingest stop cleared", meta.get("mirror_ingest_stopped") is False)
        assert_true("stale cleared", meta.get("mirror_stale") is False)
        assert_true("error cleared", "mirror_error" not in meta)


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


def test_ssh_transport_uses_injected_runner() -> None:
    dispatch_id = "acp-codex-watch-ssh"
    payload = _status_payload(dispatch_id=dispatch_id, seq=3)
    captured: list[list[str]] = []

    def capture_runner(argv: list[str]) -> tuple[int, str, str]:
        captured.append(list(argv))
        return 0, payload, ""

    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        status_path = _write_dispatch(fleet_dir, dispatch_id=dispatch_id, seq=1)
        node_entry = fleet.read_json(fleet_dir / "fleet.json")["nodes"]["build-1"]
        transport = fleet_watch.SshFleetWatchTransport(runner=capture_runner, fleet_dir=fleet_dir)
        row = fleet_watch.ingest_dispatch_mirror(
            fleet_dir,
            dispatch_id,
            fleet.read_json(status_path.parent / "meta.json"),
            transport,
            node_entry=node_entry,
        )
        assert_true("ingested", row.action == "ingested")
        assert_true("ssh invoked", len(captured) == 1)
        joined = " ".join(captured[0])
        assert_true(
            "read_status",
            "read_status_file" in joined or " cat " in f" {joined} " or "/bin/zsh" in joined,
        )


def test_ssh_transport_uses_node_state_dir_for_custom_status_path() -> None:
    dispatch_id = "acp-codex-watch-custom-state"
    custom_state_dir = "/data/goal-flight"
    payload = _status_payload(dispatch_id=dispatch_id, seq=3)
    captured: list[list[str]] = []

    def capture_runner(argv: list[str]) -> tuple[int, str, str]:
        captured.append(list(argv))
        return 0, payload, ""

    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        status_path = _write_dispatch(fleet_dir, dispatch_id=dispatch_id, seq=1)
        node_entry = fleet.read_json(fleet_dir / "fleet.json")["nodes"]["build-1"]
        node_entry["state_dir"] = custom_state_dir
        meta = fleet.read_json(status_path.parent / "meta.json")
        meta["remote_status_path"] = f"{custom_state_dir}/dispatches/{dispatch_id}/status.json"
        fleet._atomic_write_json(status_path.parent / "meta.json", meta)

        transport = fleet_watch.SshFleetWatchTransport(runner=capture_runner, fleet_dir=fleet_dir)
        row = fleet_watch.ingest_dispatch_mirror(
            fleet_dir,
            dispatch_id,
            fleet.read_json(status_path.parent / "meta.json"),
            transport,
            node_entry=node_entry,
        )

        assert_true("custom state ingest", row.ok and row.action == "ingested")
        assert_true("ssh invoked", len(captured) == 1)
        assert_true("custom status path used", custom_state_dir in " ".join(captured[0]))


def test_until_terminal_running_to_terminal() -> None:
    dispatch_id = "acp-watch-until-terminal"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        _write_dispatch(fleet_dir, dispatch_id=dispatch_id, seq=1)
        transport = FakeTransport(
            {
                dispatch_id: [
                    _status_payload(dispatch_id=dispatch_id, seq=2, state="running"),
                    _status_payload(dispatch_id=dispatch_id, seq=3, state="complete"),
                ]
            }
        )
        result = fleet_watch.watch_until_terminal(
            fleet_dir,
            dispatch_id,
            transport,
            timeout_s=10,
            interval_s=0,
            jitter_s=0,
        )
        assert_true("exit terminal", result.exit_code == 0)
        assert_true("state complete", result.state == "complete")
        assert_true("polls", result.polls == 2)


def test_until_terminal_timeout_exits_live() -> None:
    dispatch_id = "acp-watch-timeout"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        _write_dispatch(fleet_dir, dispatch_id=dispatch_id, seq=4)
        transport = FakeTransport({dispatch_id: _status_payload(dispatch_id=dispatch_id, seq=4)})
        result = fleet_watch.watch_until_terminal(
            fleet_dir,
            dispatch_id,
            transport,
            timeout_s=0,
            interval_s=0,
            jitter_s=0,
            stale_s=999,
        )
        assert_true("exit live timeout", result.exit_code == 1)
        assert_true("state running", result.state == "running")


def test_until_terminal_backfills_launch_unconfirmed_receipt() -> None:
    dispatch_id = "acp-watch-unconfirmed-live"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        status_path = _write_unconfirmed_dispatch(fleet_dir, dispatch_id=dispatch_id)
        transport = FakeTransport(
            {dispatch_id: _status_payload(dispatch_id=dispatch_id, seq=1, worker_pid=7777)}
        )

        result = fleet_watch.watch_until_terminal(
            fleet_dir,
            dispatch_id,
            transport,
            timeout_s=0,
            interval_s=0,
            jitter_s=0,
            stale_s=999,
        )

        assert_true("exit live timeout", result.exit_code == 1)
        assert_true("state running", result.state == "running")
        meta = fleet.read_json(status_path.parent / "meta.json")
        assert_true("unconfirmed cleared", meta.get("launch_unconfirmed") is False)
        assert_true("receipt pid", meta.get("launch_receipt", {}).get("remote_pid") == 7777)


def test_until_terminal_stale_dead_identity_marks_worker_dead() -> None:
    dispatch_id = "acp-watch-stale-dead"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        status_path = _write_dispatch(fleet_dir, dispatch_id=dispatch_id, seq=7)
        transport = FakeTransport(
            {dispatch_id: _status_payload(dispatch_id=dispatch_id, seq=7)},
            identity_by_dispatch={
                dispatch_id: fleet_watch.RemoteIdentityResult(ok=True, alive=False, identity=None)
            },
        )
        result = fleet_watch.watch_until_terminal(
            fleet_dir,
            dispatch_id,
            transport,
            timeout_s=10,
            interval_s=0,
            jitter_s=0,
            stale_s=0,
        )
        assert_true("exit terminal", result.exit_code == 0)
        assert_true("worker dead", result.state == "worker_dead")
        payload = json.loads(status_path.read_text())
        assert_true("mirror worker_dead", payload.get("state") == "worker_dead")
        meta = json.loads((status_path.parent / "meta.json").read_text())
        assert_true("meta row state", meta.get("row_state") == "worker_dead")
        assert_true("identity checked once", transport.identity_calls == [dispatch_id])
        assert_true("porcelain checked", len(transport.porcelain_calls) == 1)


def test_until_terminal_stale_dead_identity_dirty_worktree_marks_salvage_needed() -> None:
    dispatch_id = "acp-watch-stale-dead-dirty"
    worktree_path = f"/home/dev/.goal-flight/worktrees/{dispatch_id}"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        status_path = _write_dispatch(fleet_dir, dispatch_id=dispatch_id, seq=7)
        lock = fleet.acquire_account_lock(
            fleet_dir,
            account_key="openai/default",
            owner_dispatch_id=dispatch_id,
        )
        assert_true("lock acquired", lock.get("state") == "active")
        transport = FakeTransport(
            {dispatch_id: _status_payload(dispatch_id=dispatch_id, seq=7)},
            identity_by_dispatch={
                dispatch_id: fleet_watch.RemoteIdentityResult(ok=True, alive=False, identity=None)
            },
            porcelain_by_worktree={
                worktree_path: fleet_watch.WorktreePorcelainResult(
                    ok=True,
                    dirty=True,
                    porcelain=" M scripts/example.py",
                    worktree_path=worktree_path,
                )
            },
        )
        result = fleet_watch.watch_until_terminal(
            fleet_dir,
            dispatch_id,
            transport,
            timeout_s=10,
            interval_s=0,
            jitter_s=0,
            stale_s=0,
        )
        assert_true("exit terminal", result.exit_code == 0)
        assert_true("salvage needed", result.state == "salvage_needed")
        payload = json.loads(status_path.read_text())
        assert_true("mirror salvage_needed", payload.get("state") == "salvage_needed")
        assert_true("porcelain on mirror", payload.get("porcelain") == " M scripts/example.py")
        meta = json.loads((status_path.parent / "meta.json").read_text())
        assert_true("meta row state", meta.get("row_state") == "salvage_needed")
        lock_after = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("lock retained", lock_after is not None and lock_after.get("state") == "active")


def test_until_terminal_stale_dead_identity_porcelain_error_marks_salvage_needed() -> None:
    dispatch_id = "acp-watch-stale-dead-porcelain-error"
    worktree_path = f"/home/dev/.goal-flight/worktrees/{dispatch_id}"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        status_path = _write_dispatch(fleet_dir, dispatch_id=dispatch_id, seq=7)
        transport = FakeTransport(
            {dispatch_id: _status_payload(dispatch_id=dispatch_id, seq=7)},
            identity_by_dispatch={
                dispatch_id: fleet_watch.RemoteIdentityResult(ok=True, alive=False, identity=None)
            },
            porcelain_by_worktree={
                worktree_path: fleet_watch.WorktreePorcelainResult(
                    ok=False,
                    worktree_path=worktree_path,
                    error="ssh exit 255: connection refused",
                )
            },
        )
        result = fleet_watch.watch_until_terminal(
            fleet_dir,
            dispatch_id,
            transport,
            timeout_s=10,
            interval_s=0,
            jitter_s=0,
            stale_s=0,
        )
        assert_true("exit terminal", result.exit_code == 0)
        assert_true("salvage needed", result.state == "salvage_needed")
        payload = json.loads(status_path.read_text())
        assert_true("mirror salvage_needed", payload.get("state") == "salvage_needed")
        assert_true("not worker_dead", payload.get("state") != "worker_dead")


def _assert_self_reported_failure_dirty_worktree_salvage(state: str) -> None:
    dispatch_id = f"acp-watch-{state.replace('_', '-')}-dirty"
    worktree_path = f"/home/dev/.goal-flight/worktrees/{dispatch_id}"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        status_path = _write_dispatch(fleet_dir, dispatch_id=dispatch_id, seq=7)
        lock = fleet.acquire_account_lock(
            fleet_dir,
            account_key="openai/default",
            owner_dispatch_id=dispatch_id,
        )
        assert_true("lock acquired", lock.get("state") == "active")
        transport = FakeTransport(
            {dispatch_id: _status_payload(dispatch_id=dispatch_id, seq=8, state=state)},
            porcelain_by_worktree={
                worktree_path: fleet_watch.WorktreePorcelainResult(
                    ok=True,
                    dirty=True,
                    porcelain=" M scripts/example.py",
                    worktree_path=worktree_path,
                )
            },
        )

        result = fleet_watch.watch_until_terminal(
            fleet_dir,
            dispatch_id,
            transport,
            timeout_s=10,
            interval_s=0,
            jitter_s=0,
            stale_s=999,
        )

        assert_true("salvage needed", result.state == "salvage_needed")
        assert_true("porcelain checked", transport.porcelain_calls == [worktree_path])
        payload = json.loads(status_path.read_text())
        assert_true("mirror salvage_needed", payload.get("state") == "salvage_needed")
        assert_true("porcelain on mirror", payload.get("porcelain") == " M scripts/example.py")
        recon = fleet_reconcile.reconcile_dispatch(fleet_dir, dispatch_id, mutate=True, ssh_reachable=True)
        assert_true("reconcile holds", recon.action == "noop" and not recon.released)
        lock_after = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("lock retained", lock_after is not None and lock_after.get("state") == "active")


def test_until_terminal_self_reported_failed_dirty_worktree_marks_salvage_needed() -> None:
    _assert_self_reported_failure_dirty_worktree_salvage("failed")


def test_until_terminal_self_reported_tool_timeout_dirty_worktree_marks_salvage_needed() -> None:
    _assert_self_reported_failure_dirty_worktree_salvage("tool_timeout")


def test_until_terminal_self_reported_failed_clean_worktree_releases_lock() -> None:
    dispatch_id = "acp-watch-failed-clean"
    worktree_path = f"/home/dev/.goal-flight/worktrees/{dispatch_id}"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        status_path = _write_dispatch(fleet_dir, dispatch_id=dispatch_id, seq=7)
        lock = fleet.acquire_account_lock(
            fleet_dir,
            account_key="openai/default",
            owner_dispatch_id=dispatch_id,
        )
        assert_true("lock acquired", lock.get("state") == "active")
        transport = FakeTransport({dispatch_id: _status_payload(dispatch_id=dispatch_id, seq=8, state="failed")})

        result = fleet_watch.watch_until_terminal(
            fleet_dir,
            dispatch_id,
            transport,
            timeout_s=10,
            interval_s=0,
            jitter_s=0,
            stale_s=999,
        )

        assert_true("failed terminal", result.state == "failed")
        assert_true("porcelain checked", transport.porcelain_calls == [worktree_path])
        recon = fleet_reconcile.reconcile_dispatch(fleet_dir, dispatch_id, mutate=True, ssh_reachable=True)
        assert_true("reconcile releases", recon.action == "release_locks" and recon.released)
        lock_after = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("lock released", lock_after is None or lock_after.get("state") == "released")
        meta = fleet.read_json(status_path.parent / "meta.json")
        assert_true("meta released", meta.get("row_state") == "released")


def test_until_terminal_complete_does_not_salvage_dirty_worktree() -> None:
    dispatch_id = "acp-watch-complete-dirty"
    worktree_path = f"/home/dev/.goal-flight/worktrees/{dispatch_id}"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        _write_dispatch(fleet_dir, dispatch_id=dispatch_id, seq=7)
        transport = FakeTransport(
            {dispatch_id: _status_payload(dispatch_id=dispatch_id, seq=8, state="complete")},
            porcelain_by_worktree={
                worktree_path: fleet_watch.WorktreePorcelainResult(
                    ok=True,
                    dirty=True,
                    porcelain=" M scripts/example.py",
                    worktree_path=worktree_path,
                )
            },
        )

        result = fleet_watch.watch_until_terminal(
            fleet_dir,
            dispatch_id,
            transport,
            timeout_s=10,
            interval_s=0,
            jitter_s=0,
            stale_s=999,
        )

        assert_true("complete terminal", result.state == "complete")
        assert_true("porcelain not checked", transport.porcelain_calls == [])


def test_until_terminal_unconfirmed_no_status_live_pid_stays_unconfirmed() -> None:
    dispatch_id = "acp-watch-unconfirmed-live-pid"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        status_path = _write_unconfirmed_dispatch(
            fleet_dir,
            dispatch_id=dispatch_id,
            remote_pid=7777,
            launch_issued_at=_iso_seconds_ago(120),
        )
        lock = fleet.acquire_account_lock(
            fleet_dir,
            account_key="openai/default",
            owner_dispatch_id=dispatch_id,
        )
        assert_true("lock acquired", lock.get("state") == "active")
        transport = FakeTransport(
            {dispatch_id: fleet_watch.RemoteFetchResult(ok=False, error="remote status missing")},
            identity_by_dispatch={
                dispatch_id: fleet_watch.RemoteIdentityResult(
                    ok=True,
                    alive=True,
                    identity={"pid": 7777, "lstart": "Thu Jun 11 12:00:00 2026"},
                )
            },
        )

        result = fleet_watch.watch_until_terminal(
            fleet_dir,
            dispatch_id,
            transport,
            timeout_s=0,
            interval_s=0,
            jitter_s=0,
            stale_s=0,
        )

        assert_true("exit live", result.exit_code == 1)
        assert_true("state unconfirmed", result.state == "launch_unconfirmed")
        assert_true("status not fabricated", not status_path.exists())
        lock_after = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("lock retained", lock_after is not None and lock_after.get("state") == "active")
        meta = fleet.read_json(status_path.parent / "meta.json")
        assert_true("meta unconfirmed", meta.get("row_state") == "launch_unconfirmed")
        assert_true("pid alive", meta.get("pid_hint") == "alive")


def test_until_terminal_unconfirmed_dead_pid_before_grace_stays_unconfirmed() -> None:
    dispatch_id = "acp-watch-unconfirmed-dead-before-grace"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        status_path = _write_unconfirmed_dispatch(
            fleet_dir,
            dispatch_id=dispatch_id,
            remote_pid=7777,
            launch_issued_at=_iso_seconds_ago(1),
        )
        lock = fleet.acquire_account_lock(
            fleet_dir,
            account_key="openai/default",
            owner_dispatch_id=dispatch_id,
        )
        assert_true("lock acquired", lock.get("state") == "active")
        transport = FakeTransport(
            {dispatch_id: fleet_watch.RemoteFetchResult(ok=False, error="remote status missing")},
            identity_by_dispatch={
                dispatch_id: fleet_watch.RemoteIdentityResult(ok=True, alive=False, identity=None)
            },
        )

        result = fleet_watch.watch_until_terminal(
            fleet_dir,
            dispatch_id,
            transport,
            timeout_s=0,
            interval_s=0,
            jitter_s=0,
            stale_s=0,
        )

        assert_true("exit live", result.exit_code == 1)
        assert_true("state unconfirmed", result.state == "launch_unconfirmed")
        assert_true("status not fabricated", not status_path.exists())
        lock_after = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("lock retained", lock_after is not None and lock_after.get("state") == "active")
        meta = fleet.read_json(status_path.parent / "meta.json")
        assert_true("meta unconfirmed", meta.get("row_state") == "launch_unconfirmed")
        assert_true("pid dead noted", meta.get("pid_hint") == "dead")


def test_until_terminal_unconfirmed_dead_pid_after_grace_fails_and_releases_lock() -> None:
    dispatch_id = "acp-watch-unconfirmed-dead-after-grace"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        status_path = _write_unconfirmed_dispatch(
            fleet_dir,
            dispatch_id=dispatch_id,
            remote_pid=7777,
            launch_issued_at=_iso_seconds_ago(fleet_watch.LAUNCH_UNCONFIRMED_GRACE_S + 1),
        )
        lock = fleet.acquire_account_lock(
            fleet_dir,
            account_key="openai/default",
            owner_dispatch_id=dispatch_id,
        )
        assert_true("lock acquired", lock.get("state") == "active")
        transport = FakeTransport(
            {dispatch_id: fleet_watch.RemoteFetchResult(ok=False, error="remote status missing")},
            identity_by_dispatch={
                dispatch_id: fleet_watch.RemoteIdentityResult(ok=True, alive=False, identity=None)
            },
        )

        result = fleet_watch.watch_until_terminal(
            fleet_dir,
            dispatch_id,
            transport,
            timeout_s=10,
            interval_s=0,
            jitter_s=0,
            stale_s=0,
        )

        assert_true("exit terminal", result.exit_code == 0)
        assert_true("state failed", result.state == "failed")
        assert_true("poll grace", result.polls == fleet_watch.LAUNCH_UNCONFIRMED_GRACE_POLLS)
        payload = json.loads(status_path.read_text())
        assert_true("mirror failed", payload.get("state") == "failed")
        lock_after = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("lock released", lock_after is None or lock_after.get("state") == "released")
        meta = fleet.read_json(status_path.parent / "meta.json")
        assert_true("meta released", meta.get("row_state") == "released")


def test_until_terminal_fetch_failure_retries() -> None:
    dispatch_id = "acp-watch-fetch-retry"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        _write_dispatch(fleet_dir, dispatch_id=dispatch_id, seq=1)
        transport = FakeTransport(
            {
                dispatch_id: [
                    fleet_watch.RemoteFetchResult(ok=False, error="ssh dropped"),
                    _status_payload(dispatch_id=dispatch_id, seq=2, state="complete"),
                ]
            }
        )
        result = fleet_watch.watch_until_terminal(
            fleet_dir,
            dispatch_id,
            transport,
            timeout_s=10,
            interval_s=0,
            jitter_s=0,
        )
        assert_true("retry terminal", result.exit_code == 0)
        assert_true("poll count", result.polls == 2)


def main() -> None:
    test_ingest_advances_mirror_and_meta()
    test_ingest_rejects_mismatched_dispatch_id_payload()
    test_ingest_accepts_legacy_payload_without_dispatch_id()
    test_single_poll_backfills_launch_unconfirmed_receipt()
    test_ingest_accepts_real_dispatch_status_v1_payload()
    test_seq_regression_preserves_last_good_mirror()
    test_validate_remote_content_rejects_same_pid_legacy_replay_with_newer_timestamp()
    test_validate_remote_content_accepts_legacy_reset_with_cold_cache_and_new_pid()
    test_legacy_pid_change_with_lower_seq_resumes_ingest_after_stop()
    test_epoch_change_with_lower_seq_resumes_ingest_after_stop()
    test_real_status_writer_epoch_reset_resumes_ingest_after_recreate()
    test_successful_direct_read_clears_mirror_ingest_stop()
    test_validation_failure_does_not_truncate_mirror()
    test_sync_fleet_mirrors_batch()
    test_ssh_transport_uses_injected_runner()
    test_ssh_transport_uses_node_state_dir_for_custom_status_path()
    test_until_terminal_running_to_terminal()
    test_until_terminal_timeout_exits_live()
    test_until_terminal_backfills_launch_unconfirmed_receipt()
    test_until_terminal_stale_dead_identity_marks_worker_dead()
    test_until_terminal_stale_dead_identity_dirty_worktree_marks_salvage_needed()
    test_until_terminal_stale_dead_identity_porcelain_error_marks_salvage_needed()
    test_until_terminal_self_reported_failed_dirty_worktree_marks_salvage_needed()
    test_until_terminal_self_reported_tool_timeout_dirty_worktree_marks_salvage_needed()
    test_until_terminal_self_reported_failed_clean_worktree_releases_lock()
    test_until_terminal_complete_does_not_salvage_dirty_worktree()
    test_until_terminal_unconfirmed_no_status_live_pid_stays_unconfirmed()
    test_until_terminal_unconfirmed_dead_pid_before_grace_stays_unconfirmed()
    test_until_terminal_unconfirmed_dead_pid_after_grace_fails_and_releases_lock()
    test_until_terminal_fetch_failure_retries()
    print("OK: 30 fleet watch tests pass")


if __name__ == "__main__":
    main()
