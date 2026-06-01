#!/usr/bin/env python3
"""Hermetic tests for dispatch.py ACP routing and SIGTERM finalization."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("uses POSIX process liveness and signal semantics")

import json
import contextlib
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "scripts" / "goalflight_dispatch.py"
STATUS = ROOT / "scripts" / "goalflight_status.py"
FAKE = ROOT / "tests" / "fixtures" / "acp_fake_agent.py"
sys.path.insert(0, str(ROOT / "scripts"))


def _write_fake_codex_acp_manifest(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "codex-acp.json").write_text(
        json.dumps(
            {
                "schema": "goalflight.agent-adapter.v1",
                "support": {
                    "controller": {"capability": "supported", "fallback": "worker_only"},
                    "worker": {
                        "capability": "supported",
                        "transport": ["acp"],
                        "fallback": "tail_file",
                    },
                },
                "local_readiness_state": {
                    "controller": "probe_required",
                    "worker": "probe_required",
                    "last_probe_ids": ["python-version"],
                },
                "live_gate": {"function": "validate_adapter_gate", "default": "deny"},
                "status_contract": {"terminal_states": ["complete", "failed"], "stale_after_s": 60},
                "permission_surface": {
                    "plugin_sandbox": {},
                    "auto_approve_detection": {"strict_fail": True},
                },
                "discovery": {
                    "probes": [
                        {
                            "id": "python-version",
                            "argv": [sys.executable, "--version"],
                            "safe_for_setup": True,
                            "network": False,
                            "model_consuming": False,
                        }
                    ]
                },
                "invocation": {
                    "exec": {
                        "kind": "acp",
                        "binary": sys.executable,
                        "args": [str(FAKE)],
                        "arg_policy": {"forbidden_args": []},
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _env(tmp: Path, scenario: str) -> dict[str, str]:
    env = os.environ.copy()
    env["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
    env["GOALFLIGHT_ADAPTERS_DIR"] = str(tmp / "adapters")
    env["GOALFLIGHT_FAKE_ACP_SCENARIO"] = scenario
    return env


def _status(env: dict[str, str]) -> dict:
    proc = subprocess.run(
        [sys.executable, str(STATUS), "--json"],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return json.loads(proc.stdout)


def _wait_for(fn, timeout_s: float = 10.0):
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(0.1)
    raise AssertionError(f"condition not met before timeout; last={last!r}")


def _process_exists(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _record(payload: dict, dispatch_id: str) -> dict | None:
    for row in payload["dispatch"].get("records", []):
        if row.get("dispatch_id") == dispatch_id:
            return row
    return None


def _leases(payload: dict, dispatch_id: str) -> list[dict]:
    return [
        lease
        for lease in payload["capacity_state"].get("leases", {}).values()
        if lease.get("dispatch_id") == dispatch_id
    ]


def case_dispatch_acp_single_finalize() -> None:
    import goalflight_adapter_readiness
    import goalflight_dispatch
    import goalflight_ledger

    old_adapters_dir = goalflight_adapter_readiness.ADAPTERS_DIR
    old_finish = goalflight_ledger.cmd_finish
    old_env = {
        key: os.environ.get(key)
        for key in (
            "GOALFLIGHT_STATE_DIR",
            "GOAL_FLIGHT_PIDFILE_DIR",
            "GOALFLIGHT_ADAPTERS_DIR",
            "GOALFLIGHT_FAKE_ACP_SCENARIO",
        )
    }
    calls: list[str] = []
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            adapters = tmp / "adapters"
            _write_fake_codex_acp_manifest(adapters)
            env = _env(tmp, "echo")
            os.environ.update(env)
            goalflight_adapter_readiness.ADAPTERS_DIR = adapters

            def counting_finish(args):
                if getattr(args, "dispatch_id", None) == "acp-single-finalize":
                    calls.append(str(getattr(args, "state", "")))
                return old_finish(args)

            goalflight_ledger.cmd_finish = counting_finish
            rc = goalflight_dispatch.main(
                [
                    "--shape",
                    "acp",
                    "--agent",
                    "codex-acp",
                    "--dispatch-id",
                    "acp-single-finalize",
                    "--cwd",
                    str(ROOT),
                    "--prompt",
                    "hello",
                    "--status-json",
                    str(tmp / "status.json"),
                    "--poll-secs",
                    "0.1",
                    "--max-idle-secs",
                    "5",
                ]
            )
            assert rc == 0, rc
            assert calls == ["complete"], calls
    finally:
        goalflight_ledger.cmd_finish = old_finish
        goalflight_adapter_readiness.ADAPTERS_DIR = old_adapters_dir
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def case_dispatch_interactive_sugar_routes_codex_acp_inline() -> None:
    import goalflight_adapter_readiness
    import goalflight_dispatch

    old_adapters_dir = goalflight_adapter_readiness.ADAPTERS_DIR
    old_env = {
        key: os.environ.get(key)
        for key in (
            "GOALFLIGHT_STATE_DIR",
            "GOAL_FLIGHT_PIDFILE_DIR",
            "GOALFLIGHT_ADAPTERS_DIR",
            "GOALFLIGHT_FAKE_ACP_SCENARIO",
        )
    }
    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            adapters = tmp / "adapters"
            _write_fake_codex_acp_manifest(adapters)
            os.environ.update(_env(tmp, "echo"))
            goalflight_adapter_readiness.ADAPTERS_DIR = adapters
            status_path = tmp / "interactive.status.json"

            rc = goalflight_dispatch.main(
                [
                    "--interactive",
                    "--dispatch-id",
                    "acp-interactive-sugar",
                    "--cwd",
                    str(ROOT),
                    "--prompt",
                    "hello",
                    "--status-json",
                    str(status_path),
                    "--poll-secs",
                    "0.1",
                    "--max-idle-secs",
                    "5",
                ]
            )
            status = json.loads(status_path.read_text(encoding="utf-8"))
            assert rc == 0, rc
            assert status["agent"] == "codex-acp", status
            assert status["permission_mode"] == "inline", status
            assert status["state"] == "complete", status
    finally:
        goalflight_adapter_readiness.ADAPTERS_DIR = old_adapters_dir
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def case_dispatch_inline_permission_relay_writes_decision_and_worker_proceeds() -> None:
    import goalflight_acp_permits
    import goalflight_adapter_readiness
    import goalflight_dispatch

    old_adapters_dir = goalflight_adapter_readiness.ADAPTERS_DIR
    old_write_decision = goalflight_acp_permits.write_decision
    old_env = {
        key: os.environ.get(key)
        for key in (
            "GOALFLIGHT_STATE_DIR",
            "GOAL_FLIGHT_PIDFILE_DIR",
            "GOALFLIGHT_ADAPTERS_DIR",
            "GOALFLIGHT_FAKE_ACP_SCENARIO",
        )
    }
    decisions: list[dict[str, str | None]] = []

    def recording_write_decision(directory, key, decision, option_id=None):
        path = old_write_decision(directory, key, decision, option_id)
        decisions.append(
            {
                "directory": str(directory),
                "key": str(key),
                "decision": str(decision),
                "option_id": None if option_id is None else str(option_id),
                "path": str(path),
            }
        )
        return path

    try:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            adapters = tmp / "adapters"
            _write_fake_codex_acp_manifest(adapters)
            os.environ.update(_env(tmp, "permission_inline"))
            goalflight_adapter_readiness.ADAPTERS_DIR = adapters
            goalflight_acp_permits.write_decision = recording_write_decision
            status_path = tmp / "inline-relay.status.json"
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                rc = goalflight_dispatch.main(
                    [
                        "--interactive",
                        "--dispatch-id",
                        "acp-inline-relay",
                        "--cwd",
                        str(ROOT),
                        "--prompt",
                        "go",
                        "--status-json",
                        str(status_path),
                        "--poll-secs",
                        "0.05",
                        "--max-idle-secs",
                        "5",
                        "--permission-inline-timeout-s",
                        "5",
                    ]
                )
            out = stdout.getvalue()
            status = json.loads(status_path.read_text(encoding="utf-8"))

            assert rc == 0, (rc, out, status)
            assert decisions, "inline relay did not write a decision"
            assert decisions[0]["decision"] == "deny", decisions
            assert decisions[0]["option_id"] is None, decisions
            assert "PERMISSION-PENDING:" in out, out
            assert status["state"] == "complete", status
            assert status["permission_mode"] == "inline", status
            assert "permission:cancelled" in (status.get("result_text") or ""), status
            assert not status.get("permission_auto_declined"), status
            recorded = status.get("permission_decisions") or []
            assert recorded and recorded[0]["decision"] == "deny", recorded
            assert recorded[0]["targets_outside_cwd"] == ["/etc/hosts"], recorded
            assert "PERMISSION-PENDING" in (status.get("markers") or {}), status
    finally:
        goalflight_acp_permits.write_decision = old_write_decision
        goalflight_adapter_readiness.ADAPTERS_DIR = old_adapters_dir
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def case_dispatch_acp_sigterm_finalizes_and_reaps() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_fake_codex_acp_manifest(tmp / "adapters")
        env = _env(tmp, "idle_silent")
        dispatch_id = "acp-sigterm-finalize"
        status_path = tmp / "status.json"
        proc = subprocess.Popen(
            [
                sys.executable,
                str(DISPATCH),
                "--shape",
                "acp",
                "--agent",
                "codex-acp",
                "--dispatch-id",
                dispatch_id,
                "--cwd",
                str(ROOT),
                "--prompt",
                "stay running",
                "--status-json",
                str(status_path),
                "--poll-secs",
                "0.1",
                "--max-idle-secs",
                "30",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        worker_pid = None
        worker_orphaned = True
        try:
            running = _wait_for(
                lambda: (
                    payload
                    if status_path.exists()
                    and (payload := json.loads(status_path.read_text(encoding="utf-8")))
                    and payload.get("state") in {"running", "running_quiet"}
                    and payload.get("worker_pid")
                    else None
                ),
                timeout_s=10.0,
            )
            worker_pid = int(running["worker_pid"])
            proc.send_signal(signal.SIGTERM)
            stdout, stderr = proc.communicate(timeout=10)
            # Capture the no-orphan verdict BEFORE the finally's safety-net
            # kill — otherwise a genuinely orphaned worker is SIGKILLed by the
            # test's own cleanup and the assertion below passes vacuously. The
            # runner's SIGTERM finalizer reaps the detached worker; allow a
            # short grace for the process to actually exit before judging.
            _reap_deadline = time.monotonic() + 5.0
            while _process_exists(worker_pid) and time.monotonic() < _reap_deadline:
                time.sleep(0.1)
            worker_orphaned = _process_exists(worker_pid)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=5)
            if _process_exists(worker_pid):
                try:
                    os.kill(worker_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        assert proc.returncode == 1, f"rc={proc.returncode}\nstdout={stdout}\nstderr={stderr}"
        assert "DISPATCH-END " in stdout, stdout
        status = json.loads(status_path.read_text(encoding="utf-8"))
        assert status["state"] == "failed", status
        assert status.get("terminated_by_signal") == "SIGTERM", status
        assert status.get("worker_alive") is False, status
        assert not worker_orphaned, f"orphaned worker pid {worker_pid}"

        aggregate = _status(env)
        row = _record(aggregate, dispatch_id)
        assert row and row.get("state") == "failed", row
        assert row.get("terminal_state") == "error", row
        leases = _leases(aggregate, dispatch_id)
        assert leases, "lease missing"
        assert all(lease.get("state") == "failed" for lease in leases), leases
        assert all(lease.get("released_at") for lease in leases), leases


def case_dispatch_acp_sigterm_before_pid_update_keeps_ledger_row() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_fake_codex_acp_manifest(tmp / "adapters")
        env = _env(tmp, "idle_silent")
        env["GOALFLIGHT_TEST_ACP_BEFORE_PID_LEDGER_UPDATE_FILE"] = str(tmp / "before-pid-update")
        env["GOALFLIGHT_TEST_ACP_BEFORE_PID_LEDGER_UPDATE_S"] = "30"
        dispatch_id = "acp-ledger-before-spawn"
        status_path = tmp / "status.json"
        proc = subprocess.Popen(
            [
                sys.executable,
                str(DISPATCH),
                "--shape",
                "acp",
                "--agent",
                "codex-acp",
                "--dispatch-id",
                dispatch_id,
                "--cwd",
                str(ROOT),
                "--prompt",
                "stay running",
                "--status-json",
                str(status_path),
                "--poll-secs",
                "0.1",
                "--max-idle-secs",
                "30",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        worker_pid = None
        try:
            marker = Path(env["GOALFLIGHT_TEST_ACP_BEFORE_PID_LEDGER_UPDATE_FILE"])
            worker_pid = int(
                _wait_for(
                    lambda: marker.read_text(encoding="utf-8").strip() if marker.exists() else None,
                    timeout_s=10.0,
                )
            )
            aggregate = _status(env)
            row = _record(aggregate, dispatch_id)
            assert row, aggregate["dispatch"].get("records", [])
            assert row.get("state") == "starting", row
            assert row.get("worker_pid") is None, row

            proc.send_signal(signal.SIGTERM)
            stdout, stderr = proc.communicate(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=5)
            if _process_exists(worker_pid):
                try:
                    os.kill(worker_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        assert proc.returncode == 1, f"rc={proc.returncode}\nstdout={stdout}\nstderr={stderr}"
        assert "DISPATCH-END " in stdout, stdout
        status = json.loads(status_path.read_text(encoding="utf-8"))
        assert status["state"] == "failed", status
        assert status.get("terminated_by_signal") == "SIGTERM", status

        aggregate = _status(env)
        row = _record(aggregate, dispatch_id)
        assert row and row.get("state") == "failed", row
        assert row.get("terminal_state") == "error", row
        assert row.get("worker_pid") is None, row
        leases = _leases(aggregate, dispatch_id)
        assert leases, "lease missing"
        assert all(lease.get("state") == "failed" for lease in leases), leases
        assert all(lease.get("released_at") for lease in leases), leases


def main() -> None:
    case_dispatch_acp_single_finalize()
    case_dispatch_interactive_sugar_routes_codex_acp_inline()
    case_dispatch_inline_permission_relay_writes_decision_and_worker_proceeds()
    case_dispatch_acp_sigterm_finalizes_and_reaps()
    case_dispatch_acp_sigterm_before_pid_update_keeps_ledger_row()
    print("OK: ACP dispatch routing/SIGTERM tests pass")


if __name__ == "__main__":
    main()
