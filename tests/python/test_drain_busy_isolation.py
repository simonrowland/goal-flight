#!/usr/bin/env python3
"""One project's busy journal must not abort a shared drain pass.

A drain pass walks every project's envelopes on the shared queue. Opening
project B's journal used to raise ``JournalBusy`` and abort the whole pass,
so project A's claimed (or still-queued) work never launched. Busy is
retryable and per-project: skip B for this pass, keep draining A, and
report the skip. Structural journal errors use a distinct counter.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import goalflight_dispatch as D  # noqa: E402
import goalflight_journal as journal  # noqa: E402
import goalflight_task as task  # noqa: E402


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="dispatch drain isolation tests launch POSIX queue helpers",
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
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", os.devnull)
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_WAIT_S", "0")
    monkeypatch.setattr(D, "_export_dashboard_status_for_project", lambda *_args: None)
    monkeypatch.setattr(D, "_start_dashboard_refresh_for_project", lambda *_args: None)
    monkeypatch.setattr(D, "_release_stale_capacity_for_drain", lambda: None)
    monkeypatch.setattr(D, "_run_drain_prelaunch_hook", lambda _agents: None)


def _drain_args(queue: Path, *, limit: int = 0) -> argparse.Namespace:
    return argparse.Namespace(
        queue_dir=str(queue),
        capacity_wait_s=0.0,
        claim_stale_s=D.QUEUE_CLAIM_STALE_S,
        limit=limit,
    )


def _queue_dir(tmp_path: Path) -> Path:
    queue = tmp_path / "state" / "dispatch-queue"
    queue.mkdir(parents=True, exist_ok=True)
    return queue


def _project(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    return root


def _write_entry(
    queue: Path,
    dispatch_id: str,
    *,
    project_root: Path,
    created_at: str,
) -> Path:
    path = queue / f"{dispatch_id}.json"
    D._write_json_atomic(
        path,
        {
            "schema": D.DISPATCH_QUEUE_SCHEMA,
            "state": "queued",
            "dispatch_id": dispatch_id,
            "agent": "test-dispatch",
            "shape": "bash",
            "project_root": str(project_root),
            "process_cwd": str(project_root),
            "created_at": created_at,
            "updated_at": created_at,
            "queue_path": str(path),
            "dispatch_argv": [
                "--agent",
                "test-dispatch",
                "--dispatch-id",
                dispatch_id,
                "--cwd",
                str(project_root),
                "--",
                sys.executable,
                "-c",
                f"print('COMPLETE: {dispatch_id} — isolation test')",
            ],
            "request": {
                "agent": "test-dispatch",
                "cwd": str(project_root),
                "tail": str(project_root / f"{dispatch_id}.tail"),
                "status_json": str(project_root / f"{dispatch_id}.status.json"),
            },
        },
    )
    return path


@contextlib.contextmanager
def _hold_journal_exclusive(journal_path: Path):
    """Hold a genuine SQLite exclusive lock (b-235). WAL readers are not
    blocked by BEGIN IMMEDIATE, so this switches to DELETE then EXCLUSIVE.
    """
    connection = sqlite3.connect(str(journal_path), timeout=0, isolation_level=None)
    try:
        mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
        assert mode is not None and str(mode[0]).lower() == "delete"
        connection.execute("BEGIN EXCLUSIVE")
        yield
        connection.execute("COMMIT")
    finally:
        connection.close()


_REAL_SUBPROCESS_RUN = subprocess.run


def _capacity_blocked_run(argv, *args, **kwargs):
    """Refuse only drain-replayed dispatch children; never swallow git probes.

    ``subprocess.run`` is the drain launch seam AND the git identity probe
    used to name journal directories. A blanket stub re-keys project B onto
    a different journal file than the one we locked.
    """
    argv_list = list(argv)
    if any(str(part).endswith("goalflight_dispatch.py") for part in argv_list[:3]):
        return subprocess.CompletedProcess(
            argv_list,
            2,
            stdout="blocked_capacity\n",
            stderr="",
        )
    return _REAL_SUBPROCESS_RUN(argv, *args, **kwargs)


def _envelope_names(queue: Path) -> set[str]:
    return {path.name for path in queue.glob("*.json") if path.is_file()}


def test_queue_launch_token_hits_real_journal_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Precondition: a held exclusive lock produces JournalBusy, not a stub."""
    project = _project(tmp_path, "busy-proj")
    authority = journal.open_or_create_journal(project)
    journal_path = authority.path
    del authority
    with _hold_journal_exclusive(journal_path):
        with pytest.raises(journal.JournalBusy, match="remained busy"):
            D._queue_launch_token(
                {
                    "dispatch_id": "busy-peek",
                    "project_root": str(project),
                }
            )


def test_busy_project_is_skipped_and_other_project_still_drains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """B's journal busy first must not deny A's queued envelope."""
    queue = _queue_dir(tmp_path)
    proj_a = _project(tmp_path, "proj-a")
    proj_b = _project(tmp_path, "proj-b")
    journal.open_or_create_journal(proj_a)
    journal_b = journal.open_or_create_journal(proj_b)
    journal_b_path = journal_b.path
    del journal_b

    b1 = _write_entry(
        queue, "proj-b-one", project_root=proj_b, created_at="2026-01-01T00:00:00+00:00"
    )
    b2 = _write_entry(
        queue, "proj-b-two", project_root=proj_b, created_at="2026-01-01T00:00:01+00:00"
    )
    a1 = _write_entry(
        queue, "proj-a-one", project_root=proj_a, created_at="2026-01-01T00:00:02+00:00"
    )
    before_names = _envelope_names(queue)
    monkeypatch.setattr(D.subprocess, "run", _capacity_blocked_run)

    entry_b = json.loads(b1.read_text(encoding="utf-8"))
    assert journal.resolve_journal_path(entry_b["project_root"]) == journal_b_path
    started = time.monotonic()
    with _hold_journal_exclusive(journal_b_path):
        rc = D._cmd_drain(["--queue-dir", str(queue), "--json"])
    elapsed = time.monotonic() - started
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0, payload
    assert payload["skipped_busy"] == 2, payload
    assert payload["skipped_error"] == 0, payload
    assert payload["failed"] == 0, payload
    assert payload["launched"] == 0, payload
    by_id = {str(row.get("dispatch_id")): row for row in payload.get("details") or []}
    assert by_id["proj-b-one"]["reason"] == "journal_busy", by_id["proj-b-one"]
    assert by_id["proj-b-two"]["reason"] == "journal_busy", by_id["proj-b-two"]
    assert by_id["proj-b-one"]["state"] == "queued"
    assert by_id["proj-a-one"]["state"] == "queued", by_id["proj-a-one"]
    assert by_id["proj-a-one"]["reason"] == "capacity_unavailable", by_id["proj-a-one"]
    assert by_id["proj-b-one"]["project_root"] == str(task.resolve_project_root(str(proj_b)))
    # Second B envelope must not pay another full reader budget.
    assert elapsed < 2.5, elapsed

    after = _envelope_names(queue)
    assert after == before_names
    assert not list(queue.glob("*.json.claimed-*")), list(queue.glob("*"))
    assert b1.exists() and b2.exists() and a1.exists()
    for path in (b1, b2):
        body = json.loads(path.read_text(encoding="utf-8"))
        assert body["state"] == "queued", body
        assert body["dispatch_id"] in {"proj-b-one", "proj-b-two"}


def test_healthy_project_first_then_busy_still_reports_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _queue_dir(tmp_path)
    proj_a = _project(tmp_path, "proj-a")
    proj_b = _project(tmp_path, "proj-b")
    journal.open_or_create_journal(proj_a)
    journal_b = journal.open_or_create_journal(proj_b)
    journal_b_path = journal_b.path
    del journal_b

    a1 = _write_entry(
        queue, "proj-a-one", project_root=proj_a, created_at="2026-01-01T00:00:00+00:00"
    )
    b1 = _write_entry(
        queue, "proj-b-one", project_root=proj_b, created_at="2026-01-01T00:00:01+00:00"
    )
    monkeypatch.setattr(D.subprocess, "run", _capacity_blocked_run)

    with _hold_journal_exclusive(journal_b_path):
        payload = D._drain_queue_once(_drain_args(queue))

    by_id = {str(row.get("dispatch_id")): row for row in payload.get("details") or []}
    assert payload["skipped_busy"] == 1, payload
    assert payload["skipped_error"] == 0, payload
    assert payload["failed"] == 0, payload
    assert by_id["proj-a-one"]["reason"] == "capacity_unavailable"
    assert by_id["proj-b-one"]["reason"] == "journal_busy"
    assert a1.exists() and b1.exists()
    assert not list(queue.glob("*.json.claimed-*"))


def test_structural_journal_error_is_distinct_from_busy_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = _queue_dir(tmp_path)
    proj_a = _project(tmp_path, "proj-a")
    proj_b = _project(tmp_path, "proj-b")
    journal.open_or_create_journal(proj_a)
    journal.open_or_create_journal(proj_b)
    b_key = str(task.resolve_project_root(str(proj_b)))

    _write_entry(
        queue, "proj-b-one", project_root=proj_b, created_at="2026-01-01T00:00:00+00:00"
    )
    _write_entry(
        queue, "proj-a-one", project_root=proj_a, created_at="2026-01-01T00:00:01+00:00"
    )
    monkeypatch.setattr(D.subprocess, "run", _capacity_blocked_run)

    real_open_reader = journal.Journal.open_reader

    def open_reader(cls, project_root, **kwargs):  # type: ignore[no-untyped-def]
        root = str(task.resolve_project_root(str(project_root)))
        if root == b_key:
            raise journal.JournalIntegrityError(
                "injected structural journal failure for isolation"
            )
        return real_open_reader(project_root, **kwargs)

    monkeypatch.setattr(journal.Journal, "open_reader", classmethod(open_reader))

    before = _envelope_names(queue)
    payload = D._drain_queue_once(_drain_args(queue))
    after = _envelope_names(queue)

    by_id = {str(row.get("dispatch_id")): row for row in payload.get("details") or []}
    assert payload["skipped_busy"] == 0, payload
    assert payload["skipped_error"] == 1, payload
    assert payload["failed"] == 0, payload
    assert by_id["proj-b-one"]["reason"] == "journal_error:JournalIntegrityError"
    assert by_id["proj-a-one"]["reason"] == "capacity_unavailable"
    assert after == before
    assert not list(queue.glob("*.json.claimed-*"))


def test_cmd_drain_text_reports_skip_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    queue = _queue_dir(tmp_path)
    proj_a = _project(tmp_path, "proj-a")
    proj_b = _project(tmp_path, "proj-b")
    journal.open_or_create_journal(proj_a)
    journal_b = journal.open_or_create_journal(proj_b)
    journal_b_path = journal_b.path
    del journal_b
    _write_entry(
        queue, "proj-b-one", project_root=proj_b, created_at="2026-01-01T00:00:00+00:00"
    )
    _write_entry(
        queue, "proj-a-one", project_root=proj_a, created_at="2026-01-01T00:00:01+00:00"
    )
    monkeypatch.setattr(D.subprocess, "run", _capacity_blocked_run)

    with _hold_journal_exclusive(journal_b_path):
        rc = D._cmd_drain(["--queue-dir", str(queue)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert out.startswith("DRAIN "), out
    text = json.loads(out.split("DRAIN ", 1)[1])
    assert text["skipped_busy"] == 1, text
    assert text["skipped_error"] == 0, text
    assert text["failed"] == 0, text
    assert text["dead_claimer"] == 0, text
