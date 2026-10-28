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
import goalflight_journal as journal  # noqa: E402
import goalflight_reconcile_prelaunch as reconcile  # noqa: E402


def _isolate_state(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE_DIR", str(tmp_path / "task-state"))
    monkeypatch.setenv("GOALFLIGHT_JOURNAL_DIR", str(tmp_path / "journal-state"))
    monkeypatch.setenv("GOALFLIGHT_MESSAGES_DIR", str(tmp_path / "messages"))
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_DIR", str(tmp_path / "dispatch"))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", "/dev/null")


def _write_refusal(
    dispatch_id: str,
    project_root: Path,
    *,
    terminal_state: str | None = None,
    **overrides,
) -> dict:
    status_path = ledger.state_dir() / "dispatch" / f"{dispatch_id}.status.json"
    record = {
        "schema": ledger.SCHEMA,
        "dispatch_id": dispatch_id,
        "agent": "codex",
        "engine": "codex",
        "shape": "bash",
        "account": "default",
        "transport": "dispatch",
        "project_root": str(project_root),
        "state": "failed",
        "terminal_state": terminal_state,
        "prelaunch_failure": True,
        "worker_still_alive": False,
        "logical_session_id": dispatch_id,
        "status_path": str(status_path),
        "reason": "account resolution refused before worker launch",
    }
    record.update(overrides)
    ledger.write_record(record)
    return record


def _run(argv: list[str]) -> tuple[int, list[dict]]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
        code = reconcile.main(argv)
    rows = [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]
    return code, rows


def _finish(
    dispatch_id: str,
    *,
    state: str,
    terminal_state: str,
) -> dict:
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
        code = ledger.cmd_finish(
            argparse.Namespace(
                dispatch_id=dispatch_id,
                state=state,
                reason="recorded refusal",
                terminal_state=terminal_state,
                elapsed_s=0.0,
                worker_still_alive=False,
                prelaunch_failure=True,
            )
        )
    assert code == 0, output.getvalue()
    return json.loads(output.getvalue())


def test_selection_and_apply_are_idempotent(tmp_path: Path, monkeypatch) -> None:
    _isolate_state(monkeypatch, tmp_path)
    project = tmp_path / "repo"
    project.mkdir()

    first_id = "prelaunch-no-terminal"
    second_id = "prelaunch-existing-terminal"
    _write_refusal(first_id, project, session_id=first_id)
    _write_refusal(second_id, project, terminal_state="error")
    _finish(second_id, state="failed", terminal_state="error")

    _write_refusal(
        "has-worker-identity",
        project,
        worker_identity={"pid": 43210, "start_token": "worker-token"},
    )
    _write_refusal("has-worker-cwd", project, worker_cwd=str(project))
    authority = journal.open_or_create_journal(project)
    attempt = authority.prepare_attempt("journal-has-worker").value
    assert attempt is not None
    started = authority.start_attempt(
        attempt.attempt_id,
        attempt.launch_token,
        expected_launch_epoch=attempt.launch_epoch,
    )
    assert started.committed and started.value is not None
    attempt = started.value
    running = authority.mark_attempt_running(
        attempt.attempt_id,
        attempt.launch_token,
        launch_epoch=attempt.launch_epoch,
        worker_instance={"pid": 43211, "start_token": "journal-worker-token"},
    )
    assert running.committed
    _write_refusal("journal-has-worker", project)
    _write_refusal("no-refusal-reason", project, reason=None, error=None, outcome=None)
    status_row = _write_refusal("has-status-file", project)
    Path(status_row["status_path"]).write_text("{}\n", encoding="utf-8")

    dry_code, dry_rows = _run(["--dry-run"])
    assert dry_code == 0
    assert {row["dispatch_id"] for row in dry_rows} == {
        first_id,
        second_id,
    }
    assert all(row["mode"] == "dry-run" for row in dry_rows)
    assert all("seat" in row and "would_change" in row for row in dry_rows)
    assert ledger.read_record(first_id)["logical_session_id"] == first_id
    assert ledger.read_record(first_id)["session_id"] == first_id

    default_code, default_rows = _run([])
    assert default_code == 0
    assert {row["dispatch_id"] for row in default_rows} == {first_id, second_id}

    apply_code, apply_rows = _run(["--apply"])
    assert apply_code == 0
    applied = [row for row in apply_rows if row.get("action") == "reconciled"]
    assert {row["dispatch_id"] for row in applied} == {first_id, second_id}
    assert {row["dispatch_id"] for row in apply_rows if row.get("action") == "failed"} == set()

    first = ledger.read_record(first_id)
    assert first is not None
    assert first["state"] == "prelaunch_failed"
    assert first["terminal_state"] == "prelaunch_failed"
    assert first["prelaunch_failure"] is True
    assert first["worker_still_alive"] is False
    assert first["terminal_event_uuid"]
    first_transition_id = first["transition_id"]

    second = ledger.read_record(second_id)
    assert second is not None
    assert second["terminal_state"] == "error"
    assert second["prelaunch_failure"] is True
    assert second["worker_still_alive"] is False

    for dispatch_id in (first_id, second_id):
        status = json.loads(
            Path(ledger.read_record(dispatch_id)["status_path"]).read_text(encoding="utf-8")
        )
        assert status["prelaunch_failure"] is True
        assert status["worker_alive"] is False
        assert status["spawn_state"] == "none"
        assert "worktree_seat" not in status

    retry_code, retry_rows = _run(["--apply"])
    assert retry_code == 0
    assert retry_rows == [
        {"action": "no_candidates", "candidate_count": 0, "mode": "apply"}
    ]
    assert ledger.read_record(first_id)["transition_id"] == first_transition_id


def test_inherited_parent_session_ids_do_not_hide_the_three_prelaunch_rows(
    tmp_path: Path, monkeypatch
) -> None:
    _isolate_state(monkeypatch, tmp_path)
    project = tmp_path / "repo"
    project.mkdir()
    other_project = tmp_path / "pm2"
    other_project.mkdir()
    rows = (
        ("example-resume-3", project, "s-7"),
        ("example-resume-8", project, "s-17"),
        ("example-resume-9", other_project, "s-9"),
    )
    for dispatch_id, root, seat in rows:
        _write_refusal(
            dispatch_id,
            root,
            worktree_seat=seat,
            codex_session_id="inherited-parent-session",
            engine_session_id="inherited-parent-session",
        )

    code, selected = _run(["--dry-run"])

    assert code == 0
    assert {row["dispatch_id"] for row in selected} == {
        dispatch_id for dispatch_id, _root, _seat in rows
    }


def test_failed_worktree_prelaunch_refusal_is_reconciled_with_its_state(
    tmp_path: Path, monkeypatch
) -> None:
    _isolate_state(monkeypatch, tmp_path)
    project = tmp_path / "repo"
    project.mkdir()
    _write_refusal(
        "worktree-with-terminal",
        project,
        state="failed_worktree",
        terminal_state="error",
        reason="worktree_occupied",
    )
    _write_refusal(
        "worktree-without-terminal",
        project,
        state="failed_worktree",
        reason="worktree_occupied",
    )

    selected = {
        dispatch_id: reconcile._candidate(ledger.read_record(dispatch_id))
        for dispatch_id in ("worktree-with-terminal", "worktree-without-terminal")
    }

    assert set(selected) == {
        "worktree-with-terminal",
        "worktree-without-terminal",
    }
    assert all(row is not None for row in selected.values())
    assert all(row["state"] == "failed_worktree" for row in selected.values())
    assert all(row["terminal_state"] == "error" for row in selected.values())
