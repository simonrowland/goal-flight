"""Designed-red safety tests for explicit, bounded retention."""

from __future__ import annotations

import datetime as dt
import io
import json
import os
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_ledger  # noqa: E402
import goalflight_maintenance as maintenance  # noqa: E402
import goalflight_opencode_log_writer as log_writer  # noqa: E402
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
    state: str = "complete",
    ended_at: dt.datetime = NOW - dt.timedelta(days=8),
    worker_pid: int | None = 999_999_999,
    worker_identity: dict | None = None,
    project_root: Path | None = None,
    filename: str | None = None,
    **extra: object,
) -> dict:
    payload: dict[str, object] = {
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
    if project_root is not None:
        payload["project_root"] = str(project_root)
    payload.update(extra)
    (ledger / (filename or f"{dispatch_id}.json")).write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _dead_identity(_pid: int, _expected: dict | None) -> tuple[str, str]:
    return "dead", "fixture_dead"


def _old_record(report_fixture: tuple[Path, Path, Path, Path, Path], dispatch_id: str = "old-terminal") -> tuple[Path, Path, Path, Path, Path]:
    project, ledger, dispatch, homes, state = report_fixture
    (dispatch / f"{dispatch_id}.tail").write_text("no worker marker\n", encoding="utf-8")
    home = homes / dispatch_id
    home.mkdir()
    (home / "state.sqlite").write_bytes(b"x" * 128)
    _record(ledger, dispatch_id, project_root=project, status_path=str(dispatch / f"{dispatch_id}.status.json"))
    return project, ledger, dispatch, homes, state


def _run(fixture, **kwargs):
    project, ledger, dispatch, homes, state = fixture
    identity_probe = kwargs.pop("identity_probe", _dead_identity)
    return maintenance.run_maintenance(
        project_root=project,
        ledger_dir=ledger,
        dispatch_dir=dispatch,
        homes_dir=homes,
        state_dir=state,
        now=NOW,
        identity_probe=identity_probe,
        retention=dt.timedelta(days=7),
        max_seconds=10,
        home_dir=project.parent / "test-home",
        **kwargs,
    )


def test_dry_run_then_apply_reclaims_only_authorized_artifacts(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    project, ledger, dispatch, homes, _state = _old_record(fixture)
    dry = _run(fixture, apply=False)
    assert dry["ledger_read"] == "ok"
    assert dry["candidates"]
    assert (homes / "old-terminal").exists()
    assert (dispatch / "old-terminal.tail").exists()

    applied = _run(fixture, apply=True)
    assert applied["budget"]["deleted_files"] >= 1
    assert not (homes / "old-terminal").exists()
    assert not (dispatch / "old-terminal.tail").exists()
    receipt = project / "docs-private" / "traces" / "2026-09-15" / "old-terminal" / "RECEIPT.json"
    assert receipt.is_file()


def test_worthy_trace_source_gets_matching_receipt_before_delete(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    project, ledger, dispatch, _homes, _state = fixture
    dispatch_id = "worthy-trace"
    (dispatch / f"{dispatch_id}.tail").write_text("!COMPLETE: worthy-trace — done\n", encoding="utf-8")
    _record(ledger, dispatch_id, project_root=project)
    report = _run(fixture, apply=True)
    assert not (dispatch / f"{dispatch_id}.tail").exists()
    receipt = project / "docs-private" / "traces" / "2026-09-15" / dispatch_id / "RECEIPT.json"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["dispatch_id"] == dispatch_id
    assert payload["project_root"] == str(project)
    assert any(row.get("dispatch_id") == dispatch_id and row.get("deleted") for row in report["candidates"])


def test_resumable_codex_terminal_home_survives_normal_retention(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    project, ledger, _dispatch, homes, _state = fixture
    dispatch_id = "resumable"
    session_id = "123e4567-e89b-12d3-a456-426614174000"
    home = homes / dispatch_id
    (home / "sessions" / "2026" / "09" / "23").mkdir(parents=True)
    (home / "sessions" / "2026" / "09" / "23" / f"rollout-2026-09-23-{session_id}.jsonl").write_text("{}\n", encoding="utf-8")
    _record(ledger, dispatch_id, project_root=project, engine="codex", agent="codex", codex_session_id=session_id, codex_home=str(home))
    report = _run(fixture, apply=True)
    row = next(row for row in report["candidates"] if row.get("dispatch_id") == dispatch_id and row["component"] == "dispatch_homes")
    assert row["reason"] == "codex_resume_source_available"
    assert home.is_dir()


def test_active_codex_resume_child_keeps_parent_home(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    project, ledger, _dispatch, homes, _state = fixture
    session_id = "123e4567-e89b-12d3-a456-426614174000"
    home = homes / "parent"
    rollout_dir = home / "sessions" / "2026" / "09" / "23"
    rollout_dir.mkdir(parents=True)
    (rollout_dir / f"rollout-2026-09-23-{session_id}.jsonl").write_text("{}\n", encoding="utf-8")
    _record(ledger, "parent", project_root=project, engine="codex", agent="codex", codex_session_id=session_id, codex_home=str(home))
    _record(ledger, "child", state="running", worker_pid=None, project_root=project, engine="codex", agent="codex", codex_session_id=session_id, codex_home=str(home), parent_dispatch_id="parent")
    report = _run(fixture, apply=True)
    row = next(row for row in report["candidates"] if row.get("dispatch_id") == "parent" and row["component"] == "dispatch_homes")
    assert row["reason"] == "codex_resume_child_active"
    assert home.is_dir()


def test_live_waiter_mailbox_is_retained(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    _project, ledger, dispatch, _homes, _state = fixture
    dispatch_id = "waiting"
    identity = goalflight_ledger.process_identity(os.getpid())
    mailbox = dispatch / f"{dispatch_id}.steer.jsonl"
    mailbox.write_text(json.dumps({
        "seq": 1,
        "kind": "worker_wait_started",
        "direction": "worker_to_controller",
        "dispatch_id": dispatch_id,
        "question_id": "wait-1",
        "context": {"waiter_pid": os.getpid(), "waiter_identity": identity},
    }) + "\n", encoding="utf-8")
    _record(ledger, dispatch_id, project_root=fixture[0])
    report = _run(fixture, apply=True, identity_probe=lambda pid, expected: ("live", "fixture_live") if pid == os.getpid() else _dead_identity(pid, expected))
    row = next(row for row in report["candidates"] if row.get("path") == str(mailbox))
    assert row["reason"].startswith("live_waiter")
    assert mailbox.exists()


def test_duplicate_ledger_id_is_cannot_tell(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    project, ledger, _dispatch, homes, _state = fixture
    home = homes / "split"
    home.mkdir()
    (home / "state.sqlite").write_bytes(b"x")
    _record(ledger, "split", project_root=project, filename="one.json")
    _record(ledger, "split", state="running", project_root=project, filename="two.json")
    report = _run(fixture, apply=True)
    row = next(row for row in report["candidates"] if row.get("dispatch_id") == "split" and row["component"] == "dispatch_homes")
    assert row["reason"] == "authority_ambiguous_duplicate_ledger"
    assert home.exists()


def test_authority_is_reread_before_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _fixture(tmp_path)
    project, ledger, _dispatch, homes, _state = fixture
    home = homes / "changes"
    home.mkdir()
    (home / "state.sqlite").write_bytes(b"x")
    _record(ledger, "changes", project_root=project)
    original = maintenance._fresh_record
    calls = 0

    def change_before_read(path: Path, dispatch_id: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            payload = json.loads((path / f"{dispatch_id}.json").read_text(encoding="utf-8"))
            payload["state"] = "running"
            payload["terminal_state"] = "running"
            (path / f"{dispatch_id}.json").write_text(json.dumps(payload), encoding="utf-8")
        return original(path, dispatch_id)

    monkeypatch.setattr(maintenance, "_fresh_record", change_before_read)
    report = _run(fixture, apply=True)
    row = next(row for row in report["candidates"] if row.get("dispatch_id") == "changes" and row["component"] == "dispatch_homes")
    assert row["reason"].startswith("changed_before_delete")
    assert home.exists()


def test_symlinked_managed_root_and_receipt_destination_are_retained(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    project, ledger, dispatch, homes, _state = fixture
    outside = tmp_path / "outside"
    outside.mkdir()
    symlinked_dispatch = tmp_path / "dispatch-link"
    symlinked_dispatch.symlink_to(outside, target_is_directory=True)
    _record(ledger, "root-link", project_root=project)
    report = maintenance.run_maintenance(project_root=project, ledger_dir=ledger, dispatch_dir=symlinked_dispatch, homes_dir=homes, state_dir=fixture[-1], now=NOW, identity_probe=_dead_identity, apply=True)
    root_row = report["components"]["dispatch_dir"]["files"][0]
    assert root_row["reason"] == "managed_root_is_symlink"
    assert not list(outside.iterdir())

    (dispatch / "receipt-link.tail").write_text("no marker\n", encoding="utf-8")
    _record(ledger, "receipt-link", project_root=project)
    target = outside / "traces"
    target.mkdir()
    shutil.rmtree(project / "docs-private")
    (project / "docs-private").symlink_to(outside, target_is_directory=True)
    report = _run(fixture, apply=True)
    row = next(row for row in report["candidates"] if row.get("dispatch_id") == "receipt-link" and row["component"] == "dispatch_dir")
    assert "destination_unsafe" in row["reason"] or "archive_failed" in row["reason"]
    assert (dispatch / "receipt-link.tail").exists()


def test_each_record_uses_its_own_project_root(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fallback, ledger, dispatch, _homes, _state = fixture
    other = tmp_path / "other"
    (other / "docs-private").mkdir(parents=True)
    dispatch_id = "other-root"
    (dispatch / f"{dispatch_id}.tail").write_text("no marker\n", encoding="utf-8")
    _record(ledger, dispatch_id, project_root=other)
    _run(fixture, apply=True)
    assert (other / "docs-private" / "traces" / "2026-09-15" / dispatch_id / "RECEIPT.json").is_file()
    assert not (fallback / "docs-private" / "traces" / "2026-09-15" / dispatch_id).exists()


def test_budget_limits_apply_and_reports_reason(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    project, ledger, _dispatch, homes, _state = fixture
    for dispatch_id in ("one", "two"):
        home = homes / dispatch_id
        home.mkdir()
        (home / "state.sqlite").write_bytes(b"x" * 32)
        _record(ledger, dispatch_id, project_root=project)
    report = _run(fixture, apply=True, max_files=1)
    assert report["budget"]["deleted_files"] <= 1
    assert any(row["reason"] == "file_budget_exceeded" for row in report["candidates"])


def test_invalid_receipt_does_not_authorize_source_delete(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    project, ledger, dispatch, _homes, _state = fixture
    dispatch_id = "forged-receipt"
    (dispatch / f"{dispatch_id}.tail").write_text("no marker\n", encoding="utf-8")
    _record(ledger, dispatch_id, project_root=project)
    receipt_dir = project / "docs-private" / "traces" / "2026-09-15" / dispatch_id
    receipt_dir.mkdir(parents=True)
    (receipt_dir / "RECEIPT.json").write_text("{}", encoding="utf-8")
    report = _run(fixture, apply=True)
    row = next(row for row in report["candidates"] if row.get("dispatch_id") == dispatch_id and row["component"] == "dispatch_dir")
    assert "invalid_receipt" in row["reason"]
    assert (dispatch / f"{dispatch_id}.tail").exists()


def test_log_rotation_keeps_bounded_generations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "opencode-serve.log"
    monkeypatch.setattr(maintenance, "_path_is_open", lambda _: False)
    for generation in range(4):
        path.write_bytes(b"x" * 32 + str(generation).encode())
        assert maintenance.rotate_log(path, max_bytes=16, keep=2)["rotated"] is True
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("opencode-serve.log")) == ["opencode-serve.log", "opencode-serve.log.0", "opencode-serve.log.1"]


def test_long_lived_opencode_writer_rotates_at_write_time(tmp_path: Path) -> None:
    path = tmp_path / "opencode-serve.log"
    log_writer.copy_stream(io.BytesIO(b"a" * 17 + b"b" * 17 + b"c" * 17), path, max_bytes=16, keep=2)
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("opencode-serve.log")) == ["opencode-serve.log", "opencode-serve.log.0", "opencode-serve.log.1"]
    assert path.read_bytes() == b"c" * 3
    assert path.with_name(path.name + ".0").read_bytes() == b"b" * 2 + b"c" * 14
    assert path.with_name(path.name + ".1").read_bytes() == b"a" + b"b" * 15


def test_trace_archive_apply_requires_fresh_authority(tmp_path: Path) -> None:
    root = tmp_path / "traces"
    old_path = root / "2026-01-01" / "old-terminal"
    old_path.mkdir(parents=True)
    (old_path / "tail.log").write_bytes(b"trace")
    (old_path / "MANIFEST.json").write_text(json.dumps({"dispatch_id": "old-terminal"}), encoding="utf-8")
    record = {"dispatch_id": "old-terminal", "state": "complete", "ended_at": (NOW - dt.timedelta(days=8)).isoformat(), "worker_pid": 999_999_999}
    report = goalflight_trace_archive.retain_archives(root, records={"old-terminal": record}, now=NOW, retention=dt.timedelta(days=7), max_bytes=1024 * 1024, apply=True, identity_probe=_dead_identity, authority_reader=lambda _id: (record, None))
    assert not old_path.exists()
    assert report["reclaimed_bytes"] > 0
