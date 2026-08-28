#!/usr/bin/env python3
"""finish must refuse a conflicting explicit terminal-state on a terminal row.

Silent ok:true with the original state is the bug. Same-state retries stay
quiet. Retry ids have no ledger row; the error must name the base to act on.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_ledger as ledger  # noqa: E402


def _write_running(dispatch_id: str, project: Path) -> None:
    ledger.write_record(
        {
            "schema": ledger.SCHEMA,
            "dispatch_id": dispatch_id,
            "prompt_id": dispatch_id,
            "agent": "codex",
            "engine": "codex",
            "shape": "bash",
            "account": "default",
            "transport": "dispatch",
            "project_root": str(project),
            "state": "running",
            "started_at": ledger.utc_now(),
        }
    )


def _finish(
    dispatch_id: str,
    *,
    state: str = "complete",
    terminal_state: str | None = None,
) -> tuple[int, dict]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        code = ledger.cmd_finish(
            argparse.Namespace(
                dispatch_id=dispatch_id,
                state=state,
                reason=None,
                terminal_state=terminal_state,
                elapsed_s=None,
                worker_still_alive=False,
            )
        )
    raw = buf.getvalue()
    payload = json.loads(raw) if raw.strip() else {}
    return code, payload


def test_finish_refuses_conflicting_explicit_terminal_state(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    dispatch_id = "quota-then-supersede"
    _write_running(dispatch_id, project)

    first_code, first = _finish(
        dispatch_id, state="quota_exhausted", terminal_state="quota_exhausted"
    )
    assert first_code == 0, first
    assert first["ok"] is True
    row = json.loads(ledger.record_path(dispatch_id).read_text(encoding="utf-8"))
    assert row["terminal_state"] == "quota_exhausted"
    assert row["state"] == "quota_exhausted"

    second_code, second = _finish(
        dispatch_id, state="superseded", terminal_state="superseded"
    )
    assert second_code != 0, second
    assert second["ok"] is False
    assert second["error"] == "already_terminal"
    assert second["current_terminal_state"] == "quota_exhausted"
    assert second["requested_terminal_state"] == "superseded"
    row = json.loads(ledger.record_path(dispatch_id).read_text(encoding="utf-8"))
    assert row["terminal_state"] == "quota_exhausted"
    assert row["state"] == "quota_exhausted"


def test_finish_same_terminal_state_stays_idempotent_success(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    dispatch_id = "quota-retry-same"
    _write_running(dispatch_id, project)
    first_code, first = _finish(
        dispatch_id, state="quota_exhausted", terminal_state="quota_exhausted"
    )
    assert first_code == 0, first

    retry_code, retry = _finish(
        dispatch_id, state="quota_exhausted", terminal_state="quota_exhausted"
    )
    assert retry_code == 0, retry
    assert retry["ok"] is True
    assert retry["idempotent"] is True


def test_finish_without_explicit_terminal_state_stays_quiet(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    dispatch_id = "complete-retry-no-flag"
    _write_running(dispatch_id, project)
    first_code, first = _finish(dispatch_id)
    assert first_code == 0, first

    retry_code, retry = _finish(dispatch_id, terminal_state=None)
    assert retry_code == 0, retry
    assert retry["ok"] is True
    assert retry["idempotent"] is True


def test_finish_retry_id_names_the_base(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    base_id = "fr-d1-r3"
    child_id = "fr-d1-r3-retry-b8fa0aba"
    ledger.write_record(
        {
            "schema": ledger.SCHEMA,
            "dispatch_id": base_id,
            "agent": "codex",
            "engine": "codex",
            "shape": "bash",
            "account": "default",
            "transport": "dispatch",
            "project_root": str(project),
            "state": "quota_exhausted",
            "terminal_state": "quota_exhausted",
            "started_at": ledger.utc_now(),
            "requeue": {
                "child_id": child_id,
                "requeued_at": ledger.utc_now(),
            },
        }
    )
    code, payload = _finish(child_id, state="superseded", terminal_state="superseded")
    assert code == 1, payload
    assert payload["ok"] is False
    assert payload["error"] == "missing_dispatch"
    assert payload["dispatch_id"] == child_id
    assert payload["base_dispatch_id"] == base_id
    assert "base" in str(payload.get("hint") or "").lower()


def test_cancel_requeue_abandons_intent_and_unlinks_child(tmp_path: Path) -> None:
    import goalflight_dispatch as dispatch

    project = tmp_path / "repo"
    project.mkdir()
    parent_id = "cancel-base"
    child_id = "cancel-base-retry-bbbbbbbb"
    queue_dir = dispatch._dispatch_queue_dir()
    queue_dir.mkdir(parents=True, exist_ok=True)
    tail = tmp_path / f"{parent_id}.tail"
    tail.write_text("quota exceeded\n", encoding="utf-8")
    now = ledger.utc_now()
    entry = {
        "schema": dispatch.DISPATCH_QUEUE_SCHEMA,
        "state": "claimed",
        "dispatch_id": parent_id,
        "agent": "codex",
        "shape": "bash",
        "project_root": str(project),
        "created_at": now,
        "dispatch_argv": [
            "--agent",
            "codex",
            "--dispatch-id",
            parent_id,
            "--tail",
            str(tail),
            "--status-json",
            str(tmp_path / f"{parent_id}.status.json"),
            "--",
            sys.executable,
            "-c",
            "pass",
        ],
        "request": {
            "dispatch_id": parent_id,
            "tail": str(tail),
            "status_json": str(tmp_path / f"{parent_id}.status.json"),
        },
    }
    ledger.write_record(
        {
            "schema": ledger.SCHEMA,
            "dispatch_id": parent_id,
            "agent": "codex",
            "engine": "codex",
            "shape": "bash",
            "account": "default",
            "effective_account": "seat-r",
            "transport": "dispatch",
            "project_root": str(project),
            "state": "quota_exhausted",
            "terminal_state": "quota_exhausted",
            "started_at": now,
            "ended_at": now,
            "requeue": {"child_id": child_id, "requeued_at": now},
        }
    )
    txn = argparse.Namespace(queue_locked=True, ledger_locked=True)
    assert dispatch._maybe_requeue_terminal_claim(
        txn, entry, queue_dir=queue_dir, tail=tail
    )
    child_path = dispatch._queue_entry_path(child_id, queue_dir=queue_dir)
    assert child_path.exists()

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = ledger.cmd_cancel_requeue(argparse.Namespace(dispatch_id=parent_id))
    assert rc == 0, buf.getvalue()
    payload = json.loads(buf.getvalue())
    assert payload["ok"] is True
    assert payload["disposition"] == "abandoned"
    assert payload["idempotent"] is False
    intent = json.loads(ledger.record_path(parent_id).read_text(encoding="utf-8"))["requeue"]
    assert intent["disposition"] == "abandoned"
    assert not child_path.exists()

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = ledger.cmd_cancel_requeue(argparse.Namespace(dispatch_id=child_id))
    assert rc == 0, buf.getvalue()
    again = json.loads(buf.getvalue())
    assert again["ok"] is True
    assert again["idempotent"] is True
    assert again["dispatch_id"] == parent_id


def test_finish_cli_refuses_conflicting_terminal_state(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    dispatch_id = "cli-quota-supersede"
    _write_running(dispatch_id, project)
    first_code, first = _finish(
        dispatch_id, state="quota_exhausted", terminal_state="quota_exhausted"
    )
    assert first_code == 0, first

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        rc = ledger.main(
            [
                "finish",
                "--dispatch-id",
                dispatch_id,
                "--state",
                "superseded",
                "--terminal-state",
                "superseded",
            ]
        )
    payload = json.loads(buf.getvalue())
    assert rc == 2, payload
    assert payload["error"] == "already_terminal"
    assert payload["current_terminal_state"] == "quota_exhausted"
