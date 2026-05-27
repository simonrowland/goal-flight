"""Shared helpers for controller verification harnesses."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCHEMA = "goalflight.controller-harness.v1"

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIR = REPO_ROOT / "scripts"
WATCHER = SCRIPT_DIR / "watch-dispatch-tail.sh"


def run_cmd(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 300,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": 124,
            "stdout": exc.stdout or "",
            "stderr": ((exc.stderr or "") + "\nTIMEOUT"),
        }


def append_tail(tail_path: Path, line: str) -> None:
    tail_path.parent.mkdir(parents=True, exist_ok=True)
    with tail_path.open("a", encoding="utf-8") as handle:
        handle.write(line.rstrip() + "\n")
        handle.flush()


def doctor_snapshot(project_root: Path) -> dict[str, Any]:
    doctor = SCRIPT_DIR / "goalflight_doctor.py"
    if not doctor.is_file():
        return {"ok": False, "error": "goalflight_doctor.py missing"}
    result = run_cmd(
        [sys.executable, str(doctor), "--project-root", str(project_root), "--json"],
        timeout=120,
    )
    if not result["ok"]:
        return {"ok": False, "error": "doctor failed", "detail": result["stderr"][:500]}
    try:
        payload = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"doctor json parse: {exc}"}
    host_install = payload.get("host_goalflight_install") or {}
    return {
        "ok": True,
        "doctor_ok": payload.get("ok"),
        "codex_cli": (payload.get("codex") or {}).get("cli", {}).get("present"),
        "host_install_ok": (host_install.get("codex") or {}).get("ok"),
        "project_readiness_ok": (payload.get("project_goalflight_readiness") or {}).get("ok"),
    }


def run_bash_tail_watch(
    *,
    worker_proc: subprocess.Popen[Any],
    tail_path: Path,
    agent_label: str,
    session_id: str,
    timeout: float,
    pidfile_dir: Path,
) -> dict[str, Any]:
    env = os.environ.copy()
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(pidfile_dir)
    max_idle = max(30, int(timeout - 15))
    watcher_proc = subprocess.Popen(
        [
            "bash",
            str(WATCHER),
            "--pid",
            str(worker_proc.pid),
            "--tail",
            str(tail_path),
            "--controller-pid",
            str(os.getpid()),
            "--agent",
            agent_label,
            "--session-id",
            session_id,
            "--poll-secs",
            "1",
            "--max-idle-secs",
            str(max_idle),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        worker_rc = worker_proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        worker_proc.kill()
        worker_rc = 124
    try:
        watcher_rc = watcher_proc.wait(timeout=max(15, timeout - worker_rc if isinstance(worker_rc, int) else 0))
    except subprocess.TimeoutExpired:
        watcher_proc.kill()
        watcher_rc = 124

    tail_text = tail_path.read_text(encoding="utf-8") if tail_path.exists() else ""
    return {
        "transport": "bash_tail",
        "agent": agent_label,
        "worker_returncode": worker_rc,
        "watcher_returncode": watcher_rc,
        "tail_path": str(tail_path),
        "tail_text": tail_text,
        "complete_marker": "COMPLETE:" in tail_text,
        "blocked_marker": "BLOCKED:" in tail_text,
        "ok": worker_rc == 0 and watcher_rc == 0 and ("COMPLETE:" in tail_text),
    }


def harness_result(
    *,
    controller: str,
    scenario: str | None,
    ok: bool,
    skipped: bool = False,
    skip_reason: str | None = None,
    checks: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "ok": ok,
        "skipped": skipped,
        "controller": controller,
        "checks": checks or [],
        "elapsed_s": extra.pop("elapsed_s", None),
    }
    if scenario:
        payload["scenario"] = scenario
    if skip_reason:
        payload["skip_reason"] = skip_reason
    payload.update(extra)
    if payload.get("elapsed_s") is None:
        payload.pop("elapsed_s", None)
    return payload


def monotonic_elapsed(started: float) -> float:
    return round(time.time() - started, 2)
