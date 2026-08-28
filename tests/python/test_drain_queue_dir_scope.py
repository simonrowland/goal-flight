#!/usr/bin/env python3
"""drain --queue-dir is a SCOPE, not a restore destination (b-276).

A controller that points drain at a private/empty directory must not
materialize other projects' ledger orphans into it, must not rewrite
their ledger queue_path, and must report retained/created/relocated so
a stable file count cannot hide a composition change.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import goalflight_dispatch as D  # noqa: E402
import goalflight_ledger as L  # noqa: E402


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="dispatch drain scope tests use POSIX queue helpers",
)


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(state))
    monkeypatch.setenv("GOALFLIGHT_CODEX_STATE_DIR", str(state))
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_DIR", str(state / "dispatch"))
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE_DIR", str(tmp_path / "task-store"))
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE", str(tmp_path / "task-store"))
    monkeypatch.setenv("GOALFLIGHT_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("GOALFLIGHT_MESSAGES_DIR", str(tmp_path / "messages"))
    monkeypatch.setenv("GOALFLIGHT_WAKE_LEDGER", str(tmp_path / "wake-ledger.json"))
    monkeypatch.setenv("GOALFLIGHT_PIDFILE_DIR", str(tmp_path / "pidfiles"))
    monkeypatch.setenv("GOAL_FLIGHT_PIDFILE_DIR", str(tmp_path / "pidfiles"))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_WAIT_S", "0")
    monkeypatch.delenv("GOALFLIGHT_CONTROLLER_LABEL", raising=False)
    monkeypatch.setattr(D, "_export_dashboard_status_for_project", lambda *_args: None)
    monkeypatch.setattr(D, "_start_dashboard_refresh_for_project", lambda *_args: None)
    monkeypatch.setattr(D, "_release_stale_capacity_for_drain", lambda: None)
    monkeypatch.setattr(D, "_run_drain_prelaunch_hook", lambda _agents: None)


def _canonical_queue(tmp_path: Path) -> Path:
    queue = tmp_path / "state" / "dispatch-queue"
    queue.mkdir(parents=True, exist_ok=True)
    return queue


def _write_queue_entry(
    queue: Path,
    dispatch_id: str,
    *,
    controller_label: str | None,
    project_root: Path,
    extra: dict | None = None,
) -> Path:
    path = queue / f"{dispatch_id}.json"
    body = {
        "schema": D.DISPATCH_QUEUE_SCHEMA,
        "state": "queued",
        "dispatch_id": dispatch_id,
        "agent": "test-dispatch",
        "shape": "bash",
        "project_root": str(project_root),
        "process_cwd": str(project_root),
        "created_at": "2026-08-28T00:00:00+00:00",
        "updated_at": "2026-08-28T00:00:00+00:00",
        "queue_path": str(path),
        "dispatch_argv": [
            "--agent",
            "test-dispatch",
            "--dispatch-id",
            dispatch_id,
            "--cwd",
            str(project_root),
        ],
        "request": {
            "agent": "test-dispatch",
            "cwd": str(project_root),
            "tail": str(project_root / f"{dispatch_id}.tail"),
            "status_json": str(project_root / f"{dispatch_id}.status.json"),
            "controller_label": controller_label,
        },
    }
    if controller_label:
        body["controller_label"] = controller_label
    if extra:
        body.update(extra)
    D._write_json_atomic(path, body)
    return path


def _write_restorable_ledger(
    tmp_path: Path,
    dispatch_id: str,
    *,
    controller_label: str | None,
    project_root: Path,
    queue_path: Path,
    extra: dict | None = None,
) -> dict:
    envelope = {
        "schema": D.DISPATCH_QUEUE_SCHEMA,
        "state": "queued",
        "dispatch_id": dispatch_id,
        "agent": "test-dispatch",
        "shape": "bash",
        "project_root": str(project_root),
        "dispatch_argv": [
            "--agent",
            "test-dispatch",
            "--dispatch-id",
            dispatch_id,
            "--cwd",
            str(project_root),
        ],
        "queue_path": str(queue_path),
        "request": {
            "agent": "test-dispatch",
            "cwd": str(project_root),
            "controller_label": controller_label,
        },
    }
    if controller_label:
        envelope["controller_label"] = controller_label
    record = {
        "schema": L.SCHEMA,
        "dispatch_id": dispatch_id,
        "agent": "test-dispatch",
        "engine": "test-dispatch",
        "shape": "bash",
        "transport": "dispatch",
        "project_root": str(project_root),
        "state": "queued",
        "terminal_state": "unknown",
        "hostname": socket.gethostname(),
        "dispatch_argv": envelope["dispatch_argv"],
        "request_envelope": envelope,
        "queue_path": str(queue_path),
        "stdout_path": str(tmp_path / f"{dispatch_id}.tail"),
        "status_path": str(tmp_path / f"{dispatch_id}.status.json"),
    }
    if controller_label:
        record["controller_label"] = controller_label
    if extra:
        record.update(extra)
    L.write_record(record)
    return record


def _drain_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> dict:
    rc = D._cmd_drain(argv)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == 0, (payload, captured.err)
    return payload


def test_scoped_queue_dir_does_not_populate_foreign_controller_entries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """b-276: drain --queue-dir private must not mint other controllers' files.

    A fixture where every entry is the invoker's own would not pin the defect.
    """
    canon = _canonical_queue(tmp_path)
    private = tmp_path / "private-queue"
    private.mkdir()
    own_root = tmp_path / "pm2-engine"
    other_root = tmp_path / "other-project"
    own_root.mkdir()
    other_root.mkdir()

    foreign_id = "t811-fix4c"
    foreign_path = _write_queue_entry(
        canon,
        foreign_id,
        controller_label="other-ctrl",
        project_root=other_root,
    )
    _write_restorable_ledger(
        tmp_path,
        foreign_id,
        controller_label="other-ctrl",
        project_root=other_root,
        queue_path=foreign_path,
    )
    own_id = "pm2-own-1"
    _write_queue_entry(
        private,
        own_id,
        controller_label="pm2-engine",
        project_root=own_root,
        extra={"not_before": "2099-01-01T00:00:00+00:00"},
    )

    before_canon = {path.name: path.read_text(encoding="utf-8") for path in canon.glob("*.json")}
    rc = D._cmd_drain(
        ["--queue-dir", str(private), "--claim-stale-s", "0", "--json"]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert (private / f"{foreign_id}.json").exists() is False, list(private.iterdir())
    assert rc == 0, (payload, captured.err)
    assert foreign_path.exists(), "scoped drain removed the canonical carrier"
    assert before_canon[foreign_path.name] == foreign_path.read_text(encoding="utf-8")
    row = json.loads(L.record_path(foreign_id).read_text(encoding="utf-8"))
    assert Path(str(row.get("queue_path"))).resolve() == foreign_path.resolve(), row
    assert payload["created"] == 0, payload
    assert payload["relocated"] == 0, payload
    assert payload["retained"] >= 1, payload
    assert payload["queue_dir_scope"] is True, payload
    owners = (payload.get("queue_mutations") or {}).get("by_controller") or {}
    assert owners.get("other-ctrl", {}).get("retained", 0) >= 1, payload
    assert payload["launched"] == 0, payload


def test_unknown_owner_ledger_orphan_is_retained_not_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    canon = _canonical_queue(tmp_path)
    private = tmp_path / "private-queue"
    private.mkdir()
    other_root = tmp_path / "mystery-project"
    other_root.mkdir()
    unknown_id = "unknown-owner-1"
    queue_path = _write_queue_entry(
        canon,
        unknown_id,
        controller_label=None,
        project_root=other_root,
    )
    _write_restorable_ledger(
        tmp_path,
        unknown_id,
        controller_label=None,
        project_root=other_root,
        queue_path=queue_path,
    )

    rc = D._cmd_drain(
        ["--queue-dir", str(private), "--claim-stale-s", "0", "--json"]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert (private / f"{unknown_id}.json").exists() is False, list(private.iterdir())
    assert rc == 0, (payload, captured.err)
    assert queue_path.exists()
    assert payload["created"] == 0, payload
    assert payload["relocated"] == 0, payload
    assert payload["retained"] >= 1, payload
    retained_reasons = {
        item.get("reason")
        for item in (payload.get("queue_mutations") or {}).get("details") or []
        if item.get("dispatch_id") == unknown_id
    }
    assert "unknown_owner" in retained_reasons, payload


def test_scoped_dir_retains_foreign_controller_files_already_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Files a controller did not own, sitting in their private dir, stay put."""
    _canonical_queue(tmp_path)
    private = tmp_path / "private-queue"
    private.mkdir()
    own_root = tmp_path / "pm2-engine"
    other_root = tmp_path / "other-project"
    own_root.mkdir()
    other_root.mkdir()
    own_path = _write_queue_entry(
        private,
        "mine",
        controller_label="pm2-engine",
        project_root=own_root,
        extra={"not_before": "2099-01-01T00:00:00+00:00"},
    )
    foreign_path = _write_queue_entry(
        private,
        "theirs",
        controller_label="other-ctrl",
        project_root=other_root,
        extra={"not_before": "2099-01-01T00:00:00+00:00"},
    )
    foreign_bytes = foreign_path.read_bytes()
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_LABEL", "pm2-engine")

    payload = _drain_json(
        ["--queue-dir", str(private), "--json"],
        capsys,
    )
    assert foreign_path.exists()
    assert foreign_path.read_bytes() == foreign_bytes
    assert not list(private.glob("theirs.json.claimed-*")), list(private.iterdir())
    assert payload["retained"] >= 1, payload
    assert any(
        item.get("dispatch_id") == "theirs"
        and item.get("reason") == "foreign_controller_label"
        for item in payload.get("details") or []
    ), payload["details"]
    assert own_path.exists() or list(private.glob("mine.json*"))


def test_dispatch_id_launches_only_named_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    queue = _canonical_queue(tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    keep = _write_queue_entry(
        queue, "keep-me", controller_label="pm2-engine", project_root=root
    )
    target = _write_queue_entry(
        queue, "only-me", controller_label="pm2-engine", project_root=root
    )
    keep_bytes = keep.read_bytes()
    claimed: list[Path] = []

    def _capture_claim(path: Path) -> Path | None:
        claimed.append(path)
        return None

    monkeypatch.setattr(D, "_claim_queue_entry", _capture_claim)
    payload = _drain_json(
        ["--queue-dir", str(queue), "--dispatch-id", "only-me", "--json"],
        capsys,
    )
    assert keep.exists()
    assert keep.read_bytes() == keep_bytes
    assert [path.name for path in claimed] == [target.name]
    assert payload.get("dispatch_ids") == ["only-me"], payload
    assert payload["created"] == 0, payload


def test_canonical_restore_reports_created_not_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    queue = _canonical_queue(tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    dispatch_id = "restore-created"
    target = queue / f"{dispatch_id}.json"
    _write_restorable_ledger(
        tmp_path,
        dispatch_id,
        controller_label="pm2-engine",
        project_root=root,
        queue_path=target,
    )
    monkeypatch.setattr(D, "_claim_queue_entry", lambda _path: None)
    payload = _drain_json(
        ["--queue-dir", str(queue), "--claim-stale-s", "0", "--json"],
        capsys,
    )
    assert target.exists(), list(queue.iterdir())
    assert payload["created"] >= 1, payload
    assert payload["relocated"] == 0, payload
    owners = (payload.get("queue_mutations") or {}).get("by_controller") or {}
    assert owners.get("pm2-engine", {}).get("created", 0) >= 1, payload


def test_text_drain_line_names_retained_and_owners(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    canon = _canonical_queue(tmp_path)
    private = tmp_path / "private-queue"
    private.mkdir()
    other_root = tmp_path / "other-project"
    other_root.mkdir()
    foreign_id = "reports-live"
    path = _write_queue_entry(
        canon,
        foreign_id,
        controller_label="pm2-reports",
        project_root=other_root,
    )
    _write_restorable_ledger(
        tmp_path,
        foreign_id,
        controller_label="pm2-reports",
        project_root=other_root,
        queue_path=path,
    )
    rc = D._cmd_drain(
        ["--queue-dir", str(private), "--claim-stale-s", "0"]
    )
    captured = capsys.readouterr()
    assert rc == 0, captured
    assert (private / f"{foreign_id}.json").exists() is False, list(private.iterdir())
    line = captured.out
    summary = json.loads(line[line.index("{") :])
    assert summary["launched"] == 0
    assert summary["created"] == 0
    assert summary["relocated"] == 0
    assert summary["retained"] >= 1
    assert summary["retained_by_controller"].get("pm2-reports", 0) >= 1
