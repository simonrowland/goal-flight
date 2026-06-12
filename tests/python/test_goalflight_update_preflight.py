#!/usr/bin/env python3
"""Hermetic tests for /goal-flight update idle preflight."""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT = ROOT / "scripts" / "goalflight_update_preflight.py"
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_capacity
import goalflight_ledger
import goalflight_update_preflight as preflight


def _iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


@contextlib.contextmanager
def _state_env():
    old = os.environ.get("GOALFLIGHT_STATE_DIR")
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        os.environ["GOALFLIGHT_STATE_DIR"] = str(state_dir)
        try:
            yield state_dir, {**os.environ, "GOALFLIGHT_STATE_DIR": str(state_dir)}
        finally:
            if old is None:
                os.environ.pop("GOALFLIGHT_STATE_DIR", None)
            else:
                os.environ["GOALFLIGHT_STATE_DIR"] = old


def _write_dispatch(
    state_dir: Path,
    dispatch_id: str,
    agent: str,
    *,
    state: str = "running",
    worker_pid: int | None = None,
    write_status: bool = True,
    status_age_s: float | None = None,
) -> None:
    controller_pid = os.getpid()
    if worker_pid == 0:
        worker_pid = None
        controller_pid = None
    elif worker_pid is None:
        worker_pid = os.getpid()
    status_path = state_dir / f"{dispatch_id}.status.json"
    if write_status:
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(json.dumps({"dispatch_id": dispatch_id, "state": state}) + "\n")
        if status_age_s is not None:
            then = time.time() - status_age_s
            os.utime(status_path, (then, then))
    record = {
        "schema": goalflight_ledger.SCHEMA,
        "dispatch_id": dispatch_id,
        "prompt_id": dispatch_id,
        "prompt_path": None,
        "agent": agent,
        "engine": goalflight_ledger.infer_engine(agent),
        "shape": "bash",
        "account": "default",
        "transport": "dispatch",
        "project_root": str(ROOT),
        "controller_pid": controller_pid,
        "controller_identity": goalflight_ledger.process_identity(controller_pid),
        "worker_pid": worker_pid,
        "worker_identity": goalflight_ledger.process_identity(worker_pid),
        "lease_id": f"lease-{dispatch_id}",
        "stdout_path": None,
        "stderr_path": None,
        "status_path": str(status_path),
        "state": state,
        "terminal_state": goalflight_ledger.terminal_state_for(state),
        "started_at": _iso(),
    }
    if state in {"complete", "failed", "blocked", "worker_dead"}:
        record["ended_at"] = _iso()
        record["terminal_state"] = goalflight_ledger.terminal_state_for(state)
    goalflight_ledger.write_record(record)

    capacity = goalflight_capacity.load_state()
    capacity.setdefault("leases", {})[f"lease-{dispatch_id}"] = {
        "lease_id": f"lease-{dispatch_id}",
        "dispatch_id": dispatch_id,
        "agent": agent,
        "state": "active" if state == "running" else state,
        "project_root": str(ROOT),
        "worker_pid": worker_pid,
        "controller_pid": controller_pid,
        "started_at": _iso(),
    }
    goalflight_capacity.save_state(capacity)


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PREFLIGHT), "--check-idle", "--json", *args],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _payload(proc: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(proc.stdout)


def _dead_pid() -> int:
    pid = 999999
    assert goalflight_ledger.process_identity(pid) is None
    return pid


def case_idle_state_exits_zero() -> None:
    with _state_env() as (_state_dir, env):
        proc = _run(env)
        payload = _payload(proc)
        assert proc.returncode == 0, proc
        assert payload == {
            "idle": True,
            "live_dispatches": [],
            "advice": "idle: no in-flight dispatches",
        }, payload


def case_busy_lists_live_dispatch_identity() -> None:
    with _state_env() as (state_dir, env):
        _write_dispatch(state_dir, "codex-live", "codex")
        proc = _run(env, "--agent", "codex")
        payload = _payload(proc)
        assert proc.returncode == 3, proc
        assert payload["idle"] is False, payload
        assert payload["live_dispatches"] == [
            {
                "id": "codex-live",
                "agent": "codex",
                "pid": os.getpid(),
                "status_path": str(state_dir / "codex-live.status.json"),
            }
        ], payload
        assert "drain or pass --force" in payload["advice"], payload


def case_dead_pid_nonterminal_status_does_not_block() -> None:
    with _state_env() as (state_dir, env):
        _write_dispatch(state_dir, "codex-stale-dead", "codex", worker_pid=_dead_pid())
        status_rows = goalflight_ledger.status_payload()["records"]
        assert status_rows[0]["classification"] == "stale_dead", status_rows
        proc = _run(env, "--agent", "codex")
        payload = _payload(proc)
        assert proc.returncode == 0, payload
        assert payload == {
            "idle": True,
            "live_dispatches": [],
            "advice": "idle for codex: no in-flight dispatches",
        }, payload


def case_per_cli_scoping_keeps_unrelated_binary_idle() -> None:
    with _state_env() as (state_dir, env):
        _write_dispatch(state_dir, "grok-live", "grok-code")
        codex_proc = _run(env, "--agent", "codex")
        grok_proc = _run(env, "--agent", "grok")
        assert codex_proc.returncode == 0, codex_proc.stdout
        assert _payload(codex_proc)["idle"] is True
        assert grok_proc.returncode == 3, grok_proc.stdout
        assert [row["id"] for row in _payload(grok_proc)["live_dispatches"]] == ["grok-live"]


def case_terminal_states_do_not_block_even_with_leases() -> None:
    with _state_env() as (state_dir, env):
        _write_dispatch(state_dir, "codex-complete", "codex", state="complete")
        _write_dispatch(state_dir, "codex-worker-dead", "codex", state="worker_dead")
        proc = _run(env, "--agent", "codex")
        payload = _payload(proc)
        assert proc.returncode == 0, payload
        assert payload["idle"] is True, payload


def case_ambiguous_liveness_fails_closed_busy() -> None:
    with _state_env() as (state_dir, env):
        _write_dispatch(state_dir, "codex-unknown", "codex-acp", worker_pid=0)
        proc = _run(env, "--agent", "codex")
        payload = _payload(proc)
        assert proc.returncode == 3, payload
        assert payload["idle"] is False, payload
        assert payload["live_dispatches"][0]["id"] == "codex-unknown", payload


def case_no_pid_missing_status_does_not_block() -> None:
    with _state_env() as (state_dir, env):
        _write_dispatch(
            state_dir,
            "codex-no-evidence",
            "codex-acp",
            worker_pid=0,
            write_status=False,
        )
        proc = _run(env, "--agent", "codex")
        payload = _payload(proc)
        assert proc.returncode == 0, payload
        assert payload["idle"] is True, payload
        assert payload["live_dispatches"] == [], payload


def case_no_pid_stale_status_does_not_block() -> None:
    with _state_env() as (state_dir, env):
        _write_dispatch(
            state_dir,
            "codex-stale-status",
            "codex-acp",
            worker_pid=0,
            status_age_s=getattr(preflight, "STATUS_ACTIVITY_GRACE_S", 300.0) + 30,
        )
        proc = _run(env, "--agent", "codex")
        payload = _payload(proc)
        assert proc.returncode == 0, payload
        assert payload["idle"] is True, payload
        assert payload["live_dispatches"] == [], payload


def case_reconciled_stale_dead_row_does_not_block() -> None:
    orig_payload = preflight.goalflight_status.status_payload
    try:
        preflight.goalflight_status.status_payload = lambda: {
            "dispatch": {
                "records": [
                    {
                        "dispatch_id": "codex-stale",
                        "agent": "codex",
                        "engine": "codex",
                        "classification": "stale_dead",
                        "worker_pid": _dead_pid(),
                        "status_path": "/tmp/codex-stale.status.json",
                    }
                ]
            }
        }
        assert preflight.live_dispatches("codex") == []
    finally:
        preflight.goalflight_status.status_payload = orig_payload


def case_force_override_returns_success_but_preserves_busy_payload() -> None:
    with _state_env() as (state_dir, env):
        _write_dispatch(state_dir, "claude-live", "claude-acp")
        proc = _run(env, "--agent", "claude-code-cli-acp", "--force")
        payload = _payload(proc)
        assert proc.returncode == 0, proc
        assert payload["idle"] is False, payload
        assert payload["live_dispatches"][0]["id"] == "claude-live", payload
        assert "operator accepts mixed-binary risk" in payload["advice"], payload


def case_update_command_documents_gate_wiring() -> None:
    text = (ROOT / "commands" / "update.md").read_text(encoding="utf-8")
    assert "goalflight_update_preflight.py" in text
    assert "skipped-busy" in text
    assert "--force" in text
    assert "`grok`, `grok-code`, `grok-research`, `grok-acp`" in text


def main() -> None:
    case_idle_state_exits_zero()
    case_busy_lists_live_dispatch_identity()
    case_dead_pid_nonterminal_status_does_not_block()
    case_per_cli_scoping_keeps_unrelated_binary_idle()
    case_terminal_states_do_not_block_even_with_leases()
    case_ambiguous_liveness_fails_closed_busy()
    case_no_pid_missing_status_does_not_block()
    case_no_pid_stale_status_does_not_block()
    case_reconciled_stale_dead_row_does_not_block()
    case_force_override_returns_success_but_preserves_busy_payload()
    case_update_command_documents_gate_wiring()
    print("OK: goalflight update preflight tests pass")


if __name__ == "__main__":
    main()
