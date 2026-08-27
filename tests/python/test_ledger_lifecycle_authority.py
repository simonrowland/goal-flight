#!/usr/bin/env python3
"""Ledger finish is the lifecycle authority; status.json is a heartbeat copy.

A finished ledger row must not be counted running by goalflight_status.py just
because the sidecar is still frozen at ``state: running`` with a dead pid.
cmd_finish must not grow a second writer of that sidecar field.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_ledger as ledger  # noqa: E402
import goalflight_status as status  # noqa: E402

from support import isolated_machine_env  # noqa: E402


DEAD_PID = 1_000_000_001


def _write_sidecar(path: Path, *, dispatch_id: str, state: str, worker_pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "goalflight.status.v1",
                "dispatch_id": dispatch_id,
                "state": state,
                "worker_pid": worker_pid,
                "worker_alive": state.startswith("running"),
                "updated_at": 1,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_ledger(
    *,
    dispatch_id: str,
    project: Path,
    status_path: Path,
    state: str,
    worker_pid: int,
) -> None:
    identity = ledger.process_identity(worker_pid) or {"pid": worker_pid, "comm": "codex"}
    record = {
        "schema": ledger.SCHEMA,
        "dispatch_id": dispatch_id,
        "prompt_id": dispatch_id,
        "agent": "codex",
        "engine": "codex",
        "shape": "bash",
        "account": "default",
        "transport": "dispatch",
        "project_root": str(project),
        "worker_pid": worker_pid,
        "worker_identity": identity,
        "status_path": str(status_path),
        "state": state,
        "terminal_state": ledger.terminal_state_for(state),
        "started_at": ledger.utc_now(),
    }
    ledger.write_record(record)


def _finish(dispatch_id: str) -> dict:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = ledger.cmd_finish(
            argparse.Namespace(
                dispatch_id=dispatch_id,
                state="complete",
                reason=None,
                terminal_state=None,
                elapsed_s=None,
                worker_still_alive=False,
            )
        )
    assert code == 0, buf.getvalue()
    return json.loads(buf.getvalue())


def _record(payload: dict, dispatch_id: str) -> dict:
    for row in payload["dispatch"].get("records", []):
        if row.get("dispatch_id") == dispatch_id:
            return row
    raise AssertionError(f"missing dispatch {dispatch_id}: {payload['dispatch'].get('records')}")


@contextlib.contextmanager
def _isolated_root() -> Path:
    old_env = os.environ.copy()
    with tempfile.TemporaryDirectory(prefix="t373-lifecycle-") as td:
        root = Path(td)
        env = isolated_machine_env(root)
        os.environ.clear()
        os.environ.update(old_env)
        os.environ.update(env)
        try:
            yield root
        finally:
            os.environ.clear()
            os.environ.update(old_env)


def test_process_identity_precondition_dead_pid_is_absent() -> None:
    assert ledger.process_identity(DEAD_PID) is None, (
        "precondition failed: DEAD_PID unexpectedly has a live process identity"
    )


def test_ledger_finish_does_not_rewrite_status_json() -> None:
    with _isolated_root() as root:
        project = root / "project"
        project.mkdir()
        dispatch_id = "t373-finish-sidecar"
        status_path = Path(os.environ["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.status.json"
        _write_sidecar(status_path, dispatch_id=dispatch_id, state="running", worker_pid=DEAD_PID)
        _write_ledger(
            dispatch_id=dispatch_id,
            project=project,
            status_path=status_path,
            state="running",
            worker_pid=DEAD_PID,
        )
        before = status_path.read_text(encoding="utf-8")
        payload = _finish(dispatch_id)
        after = json.loads(status_path.read_text(encoding="utf-8"))
        ledger_row = json.loads(ledger.record_path(dispatch_id).read_text(encoding="utf-8"))
        assert payload["ok"] is True, payload
        assert payload["state"] == "complete", payload
        assert ledger_row["state"] == "complete", ledger_row
        assert after["state"] == "running", after
        assert status_path.read_text(encoding="utf-8") == before


def test_status_does_not_count_finished_dispatch_as_running() -> None:
    with _isolated_root() as root:
        project = root / "project"
        project.mkdir()
        dispatch_id = "t373-status-count"
        status_path = Path(os.environ["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.status.json"
        _write_sidecar(status_path, dispatch_id=dispatch_id, state="running", worker_pid=DEAD_PID)
        _write_ledger(
            dispatch_id=dispatch_id,
            project=project,
            status_path=status_path,
            state="running",
            worker_pid=DEAD_PID,
        )
        _finish(dispatch_id)
        payload = status.status_payload()
        row = _record(payload, dispatch_id)
        assert row["state"] == "complete", row
        assert row["classification"] == "complete", row
        assert status.done_code(row) == 0, row
        running = sum(1 for item in payload["dispatch"]["records"] if status.done_code(item) == 1)
        assert running == 0, payload["dispatch"]["records"]
        done_rc = status.main(["--done", dispatch_id, "--all-projects"])
        assert done_rc == 0


def test_genuinely_running_dispatch_still_reports_running() -> None:
    with _isolated_root() as root:
        project = root / "project"
        project.mkdir()
        dispatch_id = "t373-live"
        worker_pid = os.getpid()
        status_path = Path(os.environ["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.status.json"
        _write_sidecar(status_path, dispatch_id=dispatch_id, state="running", worker_pid=worker_pid)
        _write_ledger(
            dispatch_id=dispatch_id,
            project=project,
            status_path=status_path,
            state="running",
            worker_pid=worker_pid,
        )
        payload = status.status_payload()
        row = _record(payload, dispatch_id)
        assert row["classification"] == "expected_live", row
        assert status.done_code(row) == 1, row
        done_rc = status.main(["--done", dispatch_id, "--all-projects"])
        assert done_rc == 1


def test_stale_stalled_sidecar_does_not_overlay_finished_ledger() -> None:
    with _isolated_root() as root:
        project = root / "project"
        project.mkdir()
        dispatch_id = "t373-stalled-sidecar"
        status_path = Path(os.environ["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.status.json"
        _write_sidecar(
            status_path,
            dispatch_id=dispatch_id,
            state="worker_stalled_candidate",
            worker_pid=DEAD_PID,
        )
        _write_ledger(
            dispatch_id=dispatch_id,
            project=project,
            status_path=status_path,
            state="complete",
            worker_pid=DEAD_PID,
        )
        payload = status.status_payload()
        row = _record(payload, dispatch_id)
        assert row["state"] == "complete", row
        assert row["classification"] == "complete", row
        assert status.done_code(row) == 0, row


def test_cli_status_counts_match_ledger_after_finish() -> None:
    with _isolated_root() as root:
        project = root / "project"
        project.mkdir()
        dispatch_id = "t373-cli"
        status_path = Path(os.environ["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.status.json"
        _write_sidecar(status_path, dispatch_id=dispatch_id, state="running", worker_pid=DEAD_PID)
        _write_ledger(
            dispatch_id=dispatch_id,
            project=project,
            status_path=status_path,
            state="running",
            worker_pid=DEAD_PID,
        )
        _finish(dispatch_id)
        env = os.environ.copy()
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "goalflight_status.py"), "--all-projects"],
            cwd=str(project),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        header = proc.stdout.splitlines()[0]
        assert "running0" in header, proc.stdout
        assert "done1" in header, proc.stdout
        done = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "goalflight_status.py"),
                "--done",
                dispatch_id,
                "--all-projects",
            ],
            cwd=str(project),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert done.returncode == 0, done.stdout + done.stderr


def main() -> None:
    tests = [
        test_process_identity_precondition_dead_pid_is_absent,
        test_ledger_finish_does_not_rewrite_status_json,
        test_status_does_not_count_finished_dispatch_as_running,
        test_genuinely_running_dispatch_still_reports_running,
        test_stale_stalled_sidecar_does_not_overlay_finished_ledger,
        test_cli_status_counts_match_ledger_after_finish,
    ]
    for test in tests:
        test()
    print(f"PASS tests/python/test_ledger_lifecycle_authority.py ({len(tests)} tests)")


if __name__ == "__main__":
    main()
