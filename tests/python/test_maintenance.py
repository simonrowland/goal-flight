"""Hermetic retention tests for the standalone maintenance pass."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_maintenance as maintenance  # noqa: E402
import goalflight_ledger  # noqa: E402
import goalflight_trace_archive  # noqa: E402


NOW = dt.datetime(2026, 9, 23, 12, 0, tzinfo=dt.timezone.utc)


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    project = tmp_path / "project"
    ledger = tmp_path / "state" / "runs"
    dispatch = tmp_path / "state" / "dispatch"
    homes = tmp_path / "state" / "dispatch-homes"
    state = tmp_path / "state"
    for path in (project / "docs-private" / "research", ledger, dispatch, homes):
        path.mkdir(parents=True, exist_ok=True)
    return project, ledger, dispatch, homes, state


def _record(
    ledger: Path,
    dispatch_id: str,
    *,
    state: str,
    ended_at: dt.datetime,
    worker_pid: int | None = 999_999_999,
    worker_identity: dict | None = None,
    status_path: Path | None = None,
) -> dict:
    payload = {
        "schema": "goalflight.dispatch.v1",
        "dispatch_id": dispatch_id,
        "state": state,
        "terminal_state": state,
        "ended_at": ended_at.isoformat().replace("+00:00", "Z"),
    }
    if worker_pid is not None:
        payload["worker_pid"] = worker_pid
    if worker_identity is not None:
        payload["worker_identity"] = worker_identity
    if status_path is not None:
        payload["status_path"] = str(status_path)
    (ledger / f"{dispatch_id}.json").write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _dead_identity(_pid: int, _expected: dict | None) -> tuple[str, str]:
    return "dead", "fixture_dead"


def _fixture_identity(pid: int, _expected: dict | None) -> tuple[str, str]:
    return ("live", "fixture_live") if pid == os.getpid() else ("dead", "fixture_dead")


def test_dry_run_then_apply_reclaims_only_old_terminal_artifacts(tmp_path: Path) -> None:
    project, ledger, dispatch, homes, state = _fixture(tmp_path)
    old = NOW - dt.timedelta(days=8)
    recent = NOW - dt.timedelta(days=1)

    old_status = dispatch / "old-terminal.status.json"
    old_status.write_text(json.dumps({"state": "complete"}), encoding="utf-8")
    (dispatch / "old-terminal.tail").write_bytes(b"old tail\n")
    old_home = homes / "old-terminal"
    old_home.mkdir()
    (old_home / "state.sqlite").write_bytes(b"x" * 128)
    _record(ledger, "old-terminal", state="complete", ended_at=old, status_path=old_status)

    recent_home = homes / "recent-terminal"
    recent_home.mkdir()
    (recent_home / "state.sqlite").write_bytes(b"recent")
    _record(ledger, "recent-terminal", state="complete", ended_at=recent, worker_pid=999_999_998)

    live_identity = goalflight_ledger.process_identity(os.getpid())
    live_home = homes / "live-terminal"
    live_home.mkdir()
    (live_home / "state.sqlite").write_bytes(b"live")
    _record(
        ledger,
        "live-terminal",
        state="complete",
        ended_at=old,
        worker_pid=os.getpid(),
        worker_identity=live_identity,
    )

    unknown_home = homes / "unknown-artifact"
    unknown_home.mkdir()
    (unknown_home / "state.sqlite").write_bytes(b"unknown")

    report = maintenance.run_maintenance(
        project_root=project,
        ledger_dir=ledger,
        dispatch_dir=dispatch,
        homes_dir=homes,
        state_dir=state,
        retention=dt.timedelta(days=7),
        now=NOW,
        identity_probe=_fixture_identity,
        apply=False,
    )
    assert report["ledger_read"] == "ok"
    assert report["reclaimed_bytes"] > 0
    assert old_home.exists()
    assert (dispatch / "old-terminal.tail").exists()

    maintenance.run_maintenance(
        project_root=project,
        ledger_dir=ledger,
        dispatch_dir=dispatch,
        homes_dir=homes,
        state_dir=state,
        retention=dt.timedelta(days=7),
        now=NOW,
        identity_probe=_fixture_identity,
        apply=True,
    )
    assert not old_home.exists()
    assert not (dispatch / "old-terminal.tail").exists()
    receipt = project / "docs-private" / "traces" / "2026-09-15" / "old-terminal" / "RECEIPT.json"
    assert receipt.is_file()
    assert recent_home.exists()
    assert live_home.exists()
    assert unknown_home.exists()


def test_ledger_read_failure_is_designed_red_and_deletes_nothing(tmp_path: Path) -> None:
    project, ledger, dispatch, homes, state = _fixture(tmp_path)
    old = NOW - dt.timedelta(days=8)
    old_home = homes / "old-terminal"
    old_home.mkdir()
    (old_home / "state.sqlite").write_bytes(b"x" * 128)
    (dispatch / "old-terminal.tail").write_bytes(b"tail")
    _record(ledger, "old-terminal", state="complete", ended_at=old)
    (ledger / "corrupt.json").write_text("{", encoding="utf-8")

    report = maintenance.run_maintenance(
        project_root=project,
        ledger_dir=ledger,
        dispatch_dir=dispatch,
        homes_dir=homes,
        state_dir=state,
        retention=dt.timedelta(days=7),
        now=NOW,
        identity_probe=_dead_identity,
        apply=True,
    )
    assert report["ledger_read"] == "failed"
    assert report["safety"] == "fail-closed-no-deletes"
    assert old_home.exists()
    assert (dispatch / "old-terminal.tail").exists()


def test_log_rotation_keeps_a_bounded_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "opencode-serve.log"
    monkeypatch.setattr(maintenance, "_path_is_open", lambda _: False)
    for generation in range(4):
        path.write_bytes(b"x" * 32 + str(generation).encode())
        result = maintenance.rotate_log(path, max_bytes=16, keep=2)
        assert result["rotated"] is True
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("opencode-serve.log")) == [
        "opencode-serve.log", "opencode-serve.log.0", "opencode-serve.log.1"
    ]
    assert path.with_name(path.name + ".0").read_bytes().endswith(b"3")
    assert path.with_name(path.name + ".1").read_bytes().endswith(b"2")


def test_trace_archive_retention_keeps_recent_unknown_and_pinned(tmp_path: Path) -> None:
    root = tmp_path / "traces"
    old_path = root / "2026-01-01" / "old-terminal"
    recent_path = root / "2026-09-22" / "recent-terminal"
    unknown_path = root / "2026-01-01" / "unknown"
    pinned_path = root / "2026-01-01" / "pinned"
    for path in (old_path, recent_path, unknown_path, pinned_path):
        path.mkdir(parents=True)
    for path, dispatch_id, pinned in (
        (old_path, "old-terminal", False),
        (recent_path, "recent-terminal", False),
        (unknown_path, "unknown", False),
        (pinned_path, "pinned", True),
    ):
        (path / "tail.log").write_bytes(b"trace")
        (path / "MANIFEST.json").write_text(
            json.dumps({"dispatch_id": dispatch_id, "pinned": pinned}), encoding="utf-8"
        )
    records = {
        "old-terminal": {"dispatch_id": "old-terminal", "state": "complete", "ended_at": (NOW - dt.timedelta(days=8)).isoformat(), "worker_pid": 999_999_999},
        "recent-terminal": {"dispatch_id": "recent-terminal", "state": "complete", "ended_at": (NOW - dt.timedelta(days=1)).isoformat(), "worker_pid": 999_999_998},
    }
    report = goalflight_trace_archive.retain_archives(
        root,
        records=records,
        now=NOW,
        retention=dt.timedelta(days=7),
        max_bytes=1024 * 1024,
        apply=True,
        identity_probe=_dead_identity,
    )
    assert report["reclaimed_bytes"] > 0
    assert not old_path.exists()
    assert recent_path.exists()
    assert unknown_path.exists()
    assert pinned_path.exists()
