#!/usr/bin/env python3
"""Ledger finish is the lifecycle authority; status.json is a heartbeat copy.

A finished ledger row must not be counted running by goalflight_status.py just
because the sidecar is still frozen at ``state: running`` with a dead pid.
cmd_finish must not grow a second writer of that sidecar field. Fleet-console
display follows the same rule: a newer sidecar heartbeat cannot reopen a
structurally terminal ledger row.

Derived copies written alongside a ledger/journal mutation, checked this round
(file:line is the source that was read, not a comment-only assertion):

| Reader | Lifecycle source | Same-shape drift? |
|---|---|---|
| status_payload / done_code / CLI runningN / --done | ledger ``status_payload()`` then sidecar overlay skipped when structurally terminal (`scripts/goalflight_status.py:803-821`, `:833-846`, `:1221`) | no; overlay guard holds |
| --wait ``_wait_record_from_snapshots`` | journal final/live first; else ledger terminal blocks sidecar ``state`` (`scripts/goalflight_status.py:1449-1566`, `:1554-1558`) | no; overlay guard holds |
| launcher ``_nonterminal_dispatch_reuse_reason`` | ledger ``read_records`` only (`scripts/goalflight_dispatch.py:2548-2568`, `:2646-2649`) | no; ``status_path`` is error text |
| drain ``_ledger_task_ids_advanced`` | ledger ``state`` / ``terminal_state`` (`scripts/goalflight_dispatch.py:8401-8436`) | no |
| ``_evaluate_abandoned_dispatch`` | ledger running/terminal; sidecar is process/progress evidence (`scripts/goalflight_dispatch.py:7541-7574`) | no for this fact |
| capacity leases | lease map, not sidecar (`scripts/goalflight_capacity.py:1241-1256`; ``_machine_row`` leases at `scripts/goalflight_fleet_console.py:2086-2106`) | not a copy |
| ``live_dispatches`` | ``status_payload()`` then ``done_code`` (`scripts/goalflight_update_preflight.py:102-113`) | inherits status overlay |
| rate pressure ``_pressure_state`` | ledger terminal wins; sidecar used only as terminal-failure fallback (`scripts/goalflight_rate_pressure.py:580-601`) | no for corpse-as-live |
| ``_nonterminal_owned_dispatches`` | ledger ``is_terminal_state`` on state/terminal_state/classification (`scripts/goalflight_session_status.py:1643-1663`) | no |
| fast-plane retention ``_record_is_terminal`` | ``_worker_display_verdict`` on the ledger record (`scripts/goalflight_fleet_console.py:736-764`, `:2385-2399`) | no; not ``_authority_snapshot`` |
| fleet ``in_flight_count`` | journal ``ATTEMPT_LIVE_STATES`` (`scripts/goalflight_fleet_console.py:1529-1550`) | no |
| fleet ``_authority_snapshot`` / ``_worker_row`` display | journal, else ledger; sidecar lifecycle ignored once the ledger row is structurally terminal (`scripts/goalflight_fleet_console.py:825-876`, `:1940-1966`) | was yes (P2); closed this round |
| dashboard ``dashboard_status_payload`` / ``live`` | ledger classify + ``done_code``; sidecar is marker/idle/tail (`scripts/goalflight_status.py:937-944`, `:1044-1101`) | no for running/finished |
| ``_dashboard_refresh_record_counts_as_live`` | ledger classify + ``done_code`` (`scripts/goalflight_dispatch.py:1722-1726`) | no |
| ``ledger.cmd_status`` | ledger ``status_payload()`` with no sidecar overlay (`scripts/goalflight_ledger.py:1381-1414`, `:1707-1708`) | no |
| history projection ``project_terminal`` | ledger record via ``_is_terminal`` / ``history_worker_row`` (`scripts/goalflight_ledger.py:1110`; `scripts/goalflight_fleet_console_history.py:114-118`, `:147-166`, `:318-340`) | no; JS history, not markdown |
| ``_wait_for_detached_watcher`` | polls sidecar ``state`` until watcher-terminal or watcher death (`scripts/goalflight_dispatch.py:1315-1344`) | sidecar, by design (watcher liveness of this launch, not ``cmd_finish``) |
| fleet-launch receipt / duplicate | sidecar + launch marker for pid liveness (`scripts/goalflight_fleet_launch_detached.py:439`, `:499-513`, `:563-577`) | different fact |
| ``reconcile_terminal_outbox`` sidecar | sidecar used only when sidecar is itself terminal (`scripts/goalflight_ledger.py:1247-1273`) | not this defect |
| wake ``coverage_status`` | waiter/doorbell registration (`scripts/goalflight_wake.py:2380-2396`) | not a copy |
| task-store exports | task completion, not dispatch lifecycle (`scripts/goalflight_fleet_console.py:2880`) | not a copy |
| markdown mirrors of dispatch lifecycle | none found (history writes ``history-data.js`` from the ledger) | none |
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

import goalflight_fleet_console as fleet  # noqa: E402
import goalflight_journal as journal  # noqa: E402
import goalflight_ledger as ledger  # noqa: E402
import goalflight_status as status  # noqa: E402

from support import isolated_machine_env  # noqa: E402


DEAD_PID = 1_000_000_001


def _write_sidecar(
    path: Path,
    *,
    dispatch_id: str,
    state: str,
    worker_pid: int,
    updated_at: object = 1,
    heartbeat_at: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "goalflight.status.v1",
        "dispatch_id": dispatch_id,
        "state": state,
        "worker_pid": worker_pid,
        "worker_alive": str(state).startswith("running"),
        "updated_at": updated_at,
    }
    if heartbeat_at is not None:
        payload["heartbeat_at"] = heartbeat_at
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
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


def _mark_attempt_running(authority: journal.Journal, dispatch_id: str) -> None:
    prepared = authority.prepare_attempt(dispatch_id)
    assert prepared.committed and prepared.value is not None, prepared
    attempt = prepared.value
    starting = authority.start_attempt(attempt.attempt_id, attempt.launch_token)
    assert starting.committed and starting.value is not None, starting
    started = starting.value
    running = authority.mark_attempt_running(
        started.attempt_id,
        started.launch_token,
        launch_epoch=started.launch_epoch,
        worker_instance={"pid": os.getpid(), "source": "t373-lifecycle"},
    )
    assert running.committed, running


def test_terminal_ledger_newer_running_sidecar_renders_terminal() -> None:
    with _isolated_root() as root:
        project = root / "project"
        project.mkdir()
        dispatch_id = "t373-fleet-display"
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
        _write_sidecar(
            status_path,
            dispatch_id=dispatch_id,
            state="running",
            worker_pid=DEAD_PID,
            updated_at=2_000_000_000,
            heartbeat_at="2030-01-01T00:00:10+00:00",
        )
        ledger_row = json.loads(ledger.record_path(dispatch_id).read_text(encoding="utf-8"))
        row = fleet._worker_row(ledger_row)
        assert row["display_state"] == "complete", row
        assert row["is_terminal"] is True, row
        assert row["classification_conflict"] is False, row
        assert row["authority_resolution"] != "status.json:newer", row


def test_running_ledger_without_terminal_row_still_renders_running() -> None:
    with _isolated_root() as root:
        project = root / "project"
        project.mkdir()
        dispatch_id = "t373-fleet-live"
        worker_pid = os.getpid()
        status_path = Path(os.environ["GOALFLIGHT_DISPATCH_DIR"]) / f"{dispatch_id}.status.json"
        _write_sidecar(
            status_path,
            dispatch_id=dispatch_id,
            state="running",
            worker_pid=worker_pid,
            heartbeat_at="2030-01-01T00:00:10+00:00",
        )
        _write_ledger(
            dispatch_id=dispatch_id,
            project=project,
            status_path=status_path,
            state="running",
            worker_pid=worker_pid,
        )
        ledger_row = json.loads(ledger.record_path(dispatch_id).read_text(encoding="utf-8"))
        row = fleet._worker_row(ledger_row)
        assert row["is_terminal"] is False, row
        assert row["display_state"] in {"running", "expected_live"}, row


def test_fleet_console_counts_ignore_sidecar_lifecycle() -> None:
    with _isolated_root() as root:
        project = root / "project"
        project.mkdir()
        authority = journal.open_or_create_journal(project)
        finished_id = "t373-count-finished"
        live_id = "t373-count-live"
        prepared = authority.prepare_attempt(finished_id)
        assert prepared.committed and prepared.value is not None, prepared
        terminal = authority.commit_terminal(
            prepared.value.attempt_id,
            terminal_state="complete",
            observation={"state": "complete", "outcome": {}},
        )
        assert terminal.committed, terminal
        _mark_attempt_running(authority, live_id)
        finished_sidecar = Path(os.environ["GOALFLIGHT_DISPATCH_DIR"]) / f"{finished_id}.status.json"
        _write_sidecar(
            finished_sidecar,
            dispatch_id=finished_id,
            state="running",
            worker_pid=DEAD_PID,
            updated_at=2_000_000_000,
            heartbeat_at="2030-01-01T00:00:10+00:00",
        )
        in_flight = fleet._journal_in_flight_count(
            journal.Journal.open_reader(project),
            controller_label="owner",
        )
        assert in_flight == 1, in_flight
        finished_record = {
            "dispatch_id": finished_id,
            "state": "complete",
            "classification": "complete",
            "terminal_state": "complete",
            "updated_at": "2020-01-01T00:00:00+00:00",
            "status_path": str(finished_sidecar),
            "project_root": str(project),
        }
        live_record = {
            "dispatch_id": live_id,
            "state": "running",
            "classification": "expected_live",
            "worker_still_alive": True,
            "project_root": str(project),
        }
        assert fleet._record_is_terminal(finished_record) is True
        assert fleet._record_is_running(finished_record) is False
        assert fleet._record_is_running(live_record) is True
        row = fleet._worker_row(finished_record)
        assert row["is_terminal"] is True, row
        assert row["display_state"] == "complete", row
        machine = fleet._machine_row(
            {
                "capacity": {"operating_cap": 12},
                "capacity_state": {"leases": {}},
                "dispatch": {"records": [finished_record, live_record]},
            }
        )
        assert machine["local_workers"] == 1, machine
        assert machine["active_leases"] == 0, machine


def main() -> None:
    tests = [
        test_process_identity_precondition_dead_pid_is_absent,
        test_ledger_finish_does_not_rewrite_status_json,
        test_status_does_not_count_finished_dispatch_as_running,
        test_genuinely_running_dispatch_still_reports_running,
        test_stale_stalled_sidecar_does_not_overlay_finished_ledger,
        test_cli_status_counts_match_ledger_after_finish,
        test_terminal_ledger_newer_running_sidecar_renders_terminal,
        test_running_ledger_without_terminal_row_still_renders_running,
        test_fleet_console_counts_ignore_sidecar_lifecycle,
    ]
    for test in tests:
        test()
    print(f"PASS tests/python/test_ledger_lifecycle_authority.py ({len(tests)} tests)")


if __name__ == "__main__":
    main()
