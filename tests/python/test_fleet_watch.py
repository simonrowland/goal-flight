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
import goalflight_fleet_watch as fleet_watch

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
) -> str:
    payload = {
        "schema": mirror.STATUS_MIRROR_SCHEMA,
        "seq": seq,
        "dispatch_id": dispatch_id,
        "state": state,
        "updated_at": "2026-05-24T12:00:00+00:00",
    }
    if worker_pid is not None:
        payload["worker_pid"] = worker_pid
        payload["worker_identity"] = {
            "pid": worker_pid,
            "lstart": "Thu Jun 11 12:00:00 2026",
            "comm": "python3",
        }
    return json.dumps(payload)


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
    ) -> None:
        self._by_dispatch = by_dispatch
        self._identity_by_dispatch = identity_by_dispatch or {}
        self.calls: list[tuple[str, str]] = []
        self.identity_calls: list[str] = []

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
                "remote_status_path": "/remote/runs/status.json",
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
    test_single_poll_backfills_launch_unconfirmed_receipt()
    test_seq_regression_preserves_last_good_mirror()
    test_seq_regression_stops_subsequent_ingest()
    test_validation_failure_does_not_truncate_mirror()
    test_sync_fleet_mirrors_batch()
    test_ssh_transport_uses_injected_runner()
    test_until_terminal_running_to_terminal()
    test_until_terminal_timeout_exits_live()
    test_until_terminal_backfills_launch_unconfirmed_receipt()
    test_until_terminal_stale_dead_identity_marks_worker_dead()
    test_until_terminal_unconfirmed_no_status_live_pid_stays_unconfirmed()
    test_until_terminal_unconfirmed_dead_pid_before_grace_stays_unconfirmed()
    test_until_terminal_unconfirmed_dead_pid_after_grace_fails_and_releases_lock()
    test_until_terminal_fetch_failure_retries()
    print("OK: fleet watch tests pass")


if __name__ == "__main__":
    main()
