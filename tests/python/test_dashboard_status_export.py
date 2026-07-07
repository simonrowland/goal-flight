#!/usr/bin/env python3
"""Regression tests for file:// dashboard data exports."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("dashboard dispatch refresh test launches POSIX workers")

import contextlib
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "scripts" / "goalflight_dispatch.py"
STATUS = ROOT / "scripts" / "goalflight_status.py"
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_ledger  # noqa: E402
import goalflight_dispatch as D  # noqa: E402
import goalflight_status as S  # noqa: E402


@contextlib.contextmanager
def _isolated_env(tmp: Path):
    old_env = os.environ.copy()
    env = old_env.copy()
    env["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
    env["GOALFLIGHT_TASK_STORE_DIR"] = str(tmp / "task-store")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
    env["GOALFLIGHT_CAPACITY_WAIT_S"] = "0"
    env["GOALFLIGHT_CAPACITY_CONF"] = "/dev/null"
    os.environ.clear()
    os.environ.update(env)
    try:
        yield env
    finally:
        os.environ.clear()
        os.environ.update(old_env)


def _payload_from_status_js(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    raw = text.split("window.GF_STATUS = ", 1)[1].rsplit(";\n", 1)[0]
    return json.loads(raw)


def _write_ledger_record(
    *,
    project: Path,
    dispatch_id: str,
    status_path: Path,
    tail: Path,
    state: str,
    terminal_state: str,
    task_ids: list[str],
    worker_pid: int | None,
) -> None:
    record = {
        "schema": goalflight_ledger.SCHEMA,
        "dispatch_id": dispatch_id,
        "agent": "test-dispatch",
        "engine": "test-dispatch",
        "shape": "bash",
        "transport": "dispatch",
        "project_root": str(project.resolve()),
        "worker_pid": worker_pid,
        "worker_identity": goalflight_ledger.process_identity(worker_pid),
        "stdout_path": str(tail),
        "status_path": str(status_path),
        "state": state,
        "terminal_state": terminal_state,
        "task_ids": task_ids,
        "started_at": "2026-07-07T00:00:00+00:00",
    }
    if terminal_state != "unknown":
        record["ended_at"] = "2026-07-07T00:01:00+00:00"
    goalflight_ledger.write_record(record)


def _write_raw_ledger_record(record: dict) -> None:
    path = goalflight_ledger.record_path(str(record["dispatch_id"]))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_export_dashboard_writes_schema_valid_running_and_terminal_dispatches() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        (project / "dashboard").mkdir(parents=True)
        with _isolated_env(tmp):
            running_tail = tmp / "running.tail"
            running_tail.write_text("STATUS: running\n" + ("x" * 250) + "\n", encoding="utf-8")
            running_status = tmp / "running.status.json"
            running_status.write_text(
                json.dumps(
                    {
                        "schema": "goalflight.status.v1",
                        "dispatch_id": "running-one",
                        "agent": "test-dispatch",
                        "state": "running",
                        "worker_pid": os.getpid(),
                        "worker_alive": True,
                        "seconds_since_event": 4.25,
                        "tail_path": str(running_tail),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            _write_ledger_record(
                project=project,
                dispatch_id="running-one",
                status_path=running_status,
                tail=running_tail,
                state="running",
                terminal_state="unknown",
                task_ids=["t-001", "b-001"],
                worker_pid=os.getpid(),
            )

            done_tail = tmp / "done.tail"
            done_tail.write_text("COMPLETE: finished fixture\n", encoding="utf-8")
            done_status = tmp / "done.status.json"
            done_status.write_text(
                json.dumps(
                    {
                        "schema": "goalflight.status.v1",
                        "dispatch_id": "done-one",
                        "agent": "test-dispatch",
                        "state": "complete",
                        "terminal_marker": {"kind": "COMPLETE", "text": "finished fixture"},
                        "tail_path": str(done_tail),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            _write_ledger_record(
                project=project,
                dispatch_id="done-one",
                status_path=done_status,
                tail=done_tail,
                state="complete",
                terminal_state="complete",
                task_ids=["t-002"],
                worker_pid=None,
            )

            exported = S.export_dashboard_status(project)
            assert exported == (project / "dashboard" / "status-data.js").resolve()
            payload = _payload_from_status_js(exported)

        assert payload["schema"] == 1
        assert payload["project_root"] == str(project.resolve())
        assert set(payload["counts"]) == {"running", "worker_finished", "worker_failed", "worker_dead", "stalled"}
        assert payload["counts"]["running"] == 1
        assert payload["counts"]["worker_finished"] == 1
        by_id = {row["dispatch_id"]: row for row in payload["dispatches"]}
        assert by_id["running-one"]["task_ids"] == ["t-001", "b-001"]
        assert by_id["running-one"]["idle_s"] == 4.2
        assert len(by_id["running-one"]["tail_last_line"]) == 200
        assert by_id["done-one"]["marker"] == {"kind": "COMPLETE", "text": "finished fixture"}


def test_export_dashboard_absent_dashboard_dir_is_noop() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        project.mkdir()
        with _isolated_env(tmp) as env:
            proc = subprocess.run(
                [sys.executable, str(STATUS), "--project", str(project), "--export-dashboard"],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == ""
        assert not (project / "dashboard" / "status-data.js").exists()


def test_status_data_write_is_atomic_on_replace_failure() -> None:
    with tempfile.TemporaryDirectory() as td:
        dashboard = Path(td)
        target = dashboard / "status-data.js"
        target.write_text("old stable file\n", encoding="utf-8")
        old_replace = S.os.replace

        def fail_replace(_src, _dst):
            raise OSError("synthetic replace failure")

        S.os.replace = fail_replace
        try:
            try:
                S._write_status_data_js(target, {"schema": 1})
            except OSError:
                pass
            else:
                raise AssertionError("expected synthetic replace failure")
        finally:
            S.os.replace = old_replace

        assert target.read_text(encoding="utf-8") == "old stable file\n"
        assert list(dashboard.glob(".status-data.js.*")) == []


def test_dashboard_export_filters_project_before_reconcile_and_reads_ledger_once(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = (tmp / "project").resolve()
        other = (tmp / "other").resolve()
        calls = {"read_records": 0}
        reconciled: list[str] = []

        def fake_read_records() -> list[dict]:
            calls["read_records"] += 1
            return [
                {
                    "schema": goalflight_ledger.SCHEMA,
                    "dispatch_id": "in-project",
                    "agent": "test-dispatch",
                    "state": "complete",
                    "terminal_state": "complete",
                    "project_root": str(project),
                    "started_at": "2026-07-07T00:00:00+00:00",
                    "task_ids": ["t-001"],
                },
                {
                    "schema": goalflight_ledger.SCHEMA,
                    "dispatch_id": "out-of-project",
                    "agent": "test-dispatch",
                    "state": "worker_dead",
                    "terminal_state": "worker_dead",
                    "project_root": str(other),
                    "started_at": "2026-07-07T00:00:00+00:00",
                    "stdout_path": str(tmp / "other.tail"),
                },
            ]

        def fake_reconcile(record: dict) -> dict:
            reconciled.append(str(record.get("dispatch_id")))
            return record

        monkeypatch.setattr(S.goalflight_ledger, "read_records", fake_read_records)
        monkeypatch.setattr(S, "_reconcile_output_tail_record", fake_reconcile)

        payload = S.dashboard_status_payload(project)

    assert calls["read_records"] == 1
    assert reconciled == ["in-project"]
    assert [row["dispatch_id"] for row in payload["dispatches"]] == ["in-project"]
    assert payload["dispatches"][0]["task_ids"] == ["t-001"]


def test_export_dashboard_parent_deleted_after_precheck_is_noop(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        (project / "dashboard").mkdir(parents=True)
        original_write = S._write_status_data_js

        def delete_parent_then_write(path: Path, payload: dict) -> None:
            shutil.rmtree(path.parent)
            original_write(path, payload)

        with _isolated_env(tmp):
            monkeypatch.setattr(S, "_write_status_data_js", delete_parent_then_write)
            assert S.export_dashboard_status(project) is None
            assert not (project / "dashboard").exists()


def test_last_nonempty_tail_line_multibyte_boundary_drops_leading_replacement() -> None:
    with tempfile.TemporaryDirectory() as td:
        tail = Path(td) / "tail.log"
        tail.write_text("é" * 40000 + "\n", encoding="utf-8")

        line = S._last_nonempty_tail_line(str(tail))

    assert line
    assert not line.startswith("\ufffd")
    assert line.startswith("é")


def test_dashboard_refresh_start_is_singleton_under_concurrent_callers(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = (tmp / "project").resolve()
        (project / "dashboard").mkdir(parents=True)
        spawned: list[int] = []
        spawn_lock = threading.Lock()

        def identity(pid: int) -> dict | None:
            if int(pid) not in spawned:
                return None
            return {
                "pid": int(pid),
                "lstart": f"started-{pid}",
                "comm": "python3",
                "args": f"{sys.executable} {DISPATCH} dashboard-refresh --project-root {project}",
            }

        class FakePopen:
            def __init__(self, _argv, **_kwargs):
                D.time.sleep(0.02)
                with spawn_lock:
                    self.pid = 9000 + len(spawned) + 1
                    spawned.append(self.pid)

        with _isolated_env(tmp):
            monkeypatch.setattr(D.subprocess, "Popen", FakePopen)
            monkeypatch.setattr(D.goalflight_compat, "pid_alive", lambda pid: int(pid) in spawned)
            monkeypatch.setattr(D.goalflight_ledger, "process_identity", identity)
            errors: list[BaseException] = []

            def start() -> None:
                try:
                    D._start_dashboard_refresh_for_project(project)
                except BaseException as exc:  # pragma: no cover - test failure path
                    errors.append(exc)

            threads = [threading.Thread(target=start) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            pidfile, _log = D._dashboard_refresh_paths(project)
            payload = json.loads(pidfile.read_text(encoding="utf-8"))

    assert errors == []
    assert spawned == [9001]
    assert payload["pid"] == 9001
    assert payload["marker"] == D._DASHBOARD_REFRESH_MARKER


def test_dashboard_refresh_wrong_identity_pidfile_is_stale(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = (tmp / "project").resolve()
        (project / "dashboard").mkdir(parents=True)
        spawned: list[int] = []
        current_pid = os.getpid()
        args = f"{sys.executable} {DISPATCH} dashboard-refresh --project-root {project}"

        def identity(pid: int) -> dict | None:
            pid = int(pid)
            if pid == current_pid:
                return {"pid": pid, "lstart": "current-start", "comm": "python3", "args": args}
            if pid in spawned:
                return {"pid": pid, "lstart": f"spawned-{pid}", "comm": "python3", "args": args}
            return None

        class FakePopen:
            def __init__(self, _argv, **_kwargs):
                self.pid = 9100
                spawned.append(self.pid)

        with _isolated_env(tmp):
            pidfile, _log = D._dashboard_refresh_paths(project)
            pidfile.parent.mkdir(parents=True, exist_ok=True)
            pidfile.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "marker": D._DASHBOARD_REFRESH_MARKER,
                        "subcommand": D._DASHBOARD_REFRESH_SUBCOMMAND,
                        "pid": current_pid,
                        "identity": {
                            "pid": current_pid,
                            "lstart": "old-reused-start",
                            "comm": "python3",
                            "args": args,
                        },
                        "project_root": str(project),
                        "project_key": D._dashboard_refresh_key(project),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            monkeypatch.setattr(D.subprocess, "Popen", FakePopen)
            monkeypatch.setattr(D.goalflight_compat, "pid_alive", lambda pid: int(pid) == current_pid or int(pid) in spawned)
            monkeypatch.setattr(D.goalflight_ledger, "process_identity", identity)

            D._start_dashboard_refresh_for_project(project)
            payload = json.loads(pidfile.read_text(encoding="utf-8"))

    assert spawned == [9100]
    assert payload["pid"] == 9100


def test_dashboard_refresh_liveness_ignores_stale_queued_rows_but_display_count_stays() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = (tmp / "project").resolve()
        (project / "dashboard").mkdir(parents=True)
        old = (
            dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(seconds=D._DASHBOARD_REFRESH_QUEUED_GRACE_S + 60)
        ).isoformat(timespec="seconds")
        with _isolated_env(tmp):
            _write_raw_ledger_record(
                {
                    "schema": goalflight_ledger.SCHEMA,
                    "dispatch_id": "stale-queued",
                    "agent": "test-dispatch",
                    "engine": "test-dispatch",
                    "shape": "bash",
                    "transport": "dispatch",
                    "project_root": str(project),
                    "state": "queued",
                    "terminal_state": "unknown",
                    "started_at": old,
                    "updated_at": old,
                }
            )
            payload = S.dashboard_status_payload(project)
            live = D._dashboard_project_has_live_dispatch(project)

    assert payload["counts"]["running"] == 1
    assert live is False


def test_dashboard_refresh_loop_honors_absolute_lifetime(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        project = (Path(td) / "project").resolve()
        (project / "dashboard").mkdir(parents=True)
        exports: list[Path] = []
        ticks = iter([0.0, 11.0])

        monkeypatch.setattr(D, "_export_dashboard_status_for_project", lambda root: exports.append(root))
        monkeypatch.setattr(D, "_dashboard_project_has_live_dispatch", lambda _root: True)
        monkeypatch.setattr(D.time, "monotonic", lambda: next(ticks))
        monkeypatch.setattr(
            D.time,
            "sleep",
            lambda _seconds: (_ for _ in ()).throw(AssertionError("sleep should not run after lifetime expiry")),
        )

        assert D._dashboard_refresh_loop(project, interval_s=1, max_lifetime_s=10) == 0

    assert exports == [project]


def test_foreground_dispatch_refreshes_dashboard_status_data() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        (project / "dashboard").mkdir(parents=True)
        with _isolated_env(tmp) as env:
            worker_code = (
                "import time; "
                "print('STATUS: dashboard worker running', flush=True); "
                "time.sleep(0.3); "
                "print('COMPLETE: dashboard worker done', flush=True)"
            )
            proc = subprocess.run(
                [
                    sys.executable,
                    str(DISPATCH),
                    "--agent",
                    "test-dispatch",
                    "--dispatch-id",
                    "dashboard-watch",
                    "--cwd",
                    str(project),
                    "--tail",
                    str(tmp / "dashboard-watch.tail"),
                    "--status-json",
                    str(tmp / "dashboard-watch.status.json"),
                    "--poll-secs",
                    "0.1",
                    "--max-idle-secs",
                    "5",
                    "--foreground",
                    "--",
                    sys.executable,
                    "-c",
                    worker_code,
                ],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
            payload = _payload_from_status_js(project / "dashboard" / "status-data.js")
            projects_index = json.loads((tmp / "task-store" / "projects.json").read_text(encoding="utf-8"))

        assert proc.returncode == 0, (proc.stdout, proc.stderr)
        row = next(row for row in payload["dispatches"] if row["dispatch_id"] == "dashboard-watch")
        assert row["task_ids"] == []
        assert row["tail_last_line"] == "COMPLETE: dashboard worker done"
        assert payload["counts"]["worker_finished"] == 1
        assert any(item["project_root"] == str(project.resolve()) for item in projects_index["projects"])
