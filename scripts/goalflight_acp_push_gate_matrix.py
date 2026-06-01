#!/usr/bin/env python3
"""Gated live ACP push-gate matrix across supported worker adapters."""

from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass, asdict
import datetime as dt
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
DISPATCH = SCRIPT_DIR / "goalflight_dispatch.py"
RUNNER = SCRIPT_DIR / "goalflight_acp_run.py"
STATUS = SCRIPT_DIR / "goalflight_status.py"
sys.path.insert(0, str(SCRIPT_DIR))

from goalflight_acp_run import agent_command, adapter_liveness_config  # noqa: E402
from goalflight_adapter_readiness import validate_acp_dispatch_readiness  # noqa: E402
import goalflight_compat  # noqa: E402


PROPERTIES = (
    "round_trip",
    "auto_permission",
    "ledger_stats",
    "held_permission",
    "locations",
    "silent_turn",
)


@dataclass(frozen=True)
class AgentSpec:
    label: str
    runner_agent: str
    timeout_s: float = 420.0
    defer_headless_failures: bool = False


@dataclass
class MatrixCell:
    agent: str
    property: str
    status: str
    detail: str
    dispatch_id: str | None = None


@dataclass
class AcpRun:
    dispatch_id: str
    payload: dict[str, Any] | None
    returncode: int | None
    stdout_tail: str
    stderr_tail: str
    elapsed_s: float
    timed_out: bool = False


AGENTS = (
    AgentSpec("codex-acp", "codex-acp"),
    AgentSpec("cursor", "cursor"),
    AgentSpec("grok", "grok"),
    AgentSpec("claude-acp", "claude-acp", timeout_s=180.0, defer_headless_failures=True),
)


ROUNDTRIP_PROMPT = "Reply exactly on its own line: COMPLETE: GF-MATRIX-ROUNDTRIP"


def _now_slug() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _tail(text: str | None, limit: int = 900) -> str:
    value = text or ""
    return value[-limit:]


def _parse_json_from_stdout(stdout: str) -> dict[str, Any] | None:
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def _matrix_env(state_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    state = state_root / "state"
    pids = state_root / "pids"
    state.mkdir(parents=True, exist_ok=True)
    pids.mkdir(parents=True, exist_ok=True)
    env["GOALFLIGHT_STATE_DIR"] = str(state)
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(pids)
    return env


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    if goalflight_compat.is_windows():
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            proc.kill()
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        return
    except (PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            proc.kill()


def _preflight(spec: AgentSpec) -> tuple[bool, str]:
    binary, args = agent_command(spec.runner_agent)
    gate = validate_acp_dispatch_readiness(spec.runner_agent, [binary, *args])
    if gate is not None:
        reason = str(gate.get("reason") or "adapter_gate_blocked")
        if reason in {"not_installed", "probe_required", "failed", "blocked"}:
            return False, f"SKIP-unavailable: {reason}"
        return False, f"FAIL-adapter-gate: {reason}"
    return True, "ready"


def _status_from_payload(payload: dict[str, Any] | None) -> str:
    if not payload:
        return "no-payload"
    return str(payload.get("state") or payload.get("terminal_state") or "unknown")


def _run_acp_case(
    spec: AgentSpec,
    *,
    state_root: Path,
    cwd: Path,
    dispatch_id: str,
    prompt: str,
    timeout_s: float,
    permission_mode: str = "auto",
    inline_timeout_s: float | None = None,
    heartbeat_interval_s: float = 1.0,
    idle_timeout_s: float = 240.0,
    max_tool_s: float = 3600.0,
) -> AcpRun:
    status_path = state_root / "statuses" / f"{dispatch_id}.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        sys.executable,
        str(RUNNER),
        "--agent",
        spec.runner_agent,
        "--cwd",
        str(cwd),
        "--dispatch-id",
        dispatch_id,
        "--prompt-text",
        prompt,
        "--status-json",
        str(status_path),
        "--permission-mode",
        permission_mode,
        "--context-mode",
        "disabled",
        "--idle-timeout",
        str(idle_timeout_s),
        "--heartbeat-interval",
        str(heartbeat_interval_s),
        "--progress-stall-s",
        "300",
        "--max-tool-s",
        str(max_tool_s),
        "--json",
    ]
    if inline_timeout_s is not None:
        argv.extend(["--permission-inline-timeout-s", str(inline_timeout_s)])
    env = _matrix_env(state_root)
    started = time.time()
    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            argv,
            cwd=ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=not goalflight_compat.is_windows(),
        )
        stdout, stderr = proc.communicate(timeout=timeout_s)
        elapsed = time.time() - started
        payload = None
        if status_path.exists():
            try:
                payload = json.loads(status_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = None
        if payload is None:
            payload = _parse_json_from_stdout(stdout)
        return AcpRun(
            dispatch_id=dispatch_id,
            payload=payload,
            returncode=proc.returncode,
            stdout_tail=_tail(stdout),
            stderr_tail=_tail(stderr),
            elapsed_s=elapsed,
        )
    except subprocess.TimeoutExpired as exc:
        if proc is not None:
            _kill_process_group(proc)
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    proc.kill()
                stdout, stderr = proc.communicate()
        else:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        payload = None
        if status_path.exists():
            try:
                payload = json.loads(status_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = None
        return AcpRun(
            dispatch_id=dispatch_id,
            payload=payload,
            returncode=None,
            stdout_tail=_tail(stdout),
            stderr_tail=_tail(stderr),
            elapsed_s=time.time() - started,
            timed_out=True,
        )


def _claude_deferred(spec: AgentSpec, run: AcpRun) -> str | None:
    if not spec.defer_headless_failures:
        return None
    status = _status_from_payload(run.payload)
    if status == "complete":
        return None
    haystack = " ".join(
        [
            status,
            json.dumps((run.payload or {}).get("error", ""), sort_keys=True),
            run.stderr_tail,
            run.stdout_tail,
        ]
    ).lower()
    needles = ("401", "unauthorized", "auth", "login", "pty", "terminal", "handshake", "timeout", "timed out")
    if run.timed_out or any(needle in haystack for needle in needles):
        return f"claude-acp deferred (headless auth/PTY): {status}"
    return None


def _is_claude_defer_cell(spec: AgentSpec, cell: MatrixCell) -> bool:
    return (
        spec.defer_headless_failures
        and cell.status == "SKIP"
        and cell.detail.startswith("claude-acp deferred")
    )


def _is_complete(run: AcpRun) -> bool:
    payload = run.payload or {}
    return run.returncode == 0 and payload.get("state") == "complete" and payload.get("ok") is True


def _round_trip(spec: AgentSpec, state_root: Path, work_root: Path) -> MatrixCell:
    cwd = work_root / spec.label / "roundtrip"
    cwd.mkdir(parents=True, exist_ok=True)
    run = _run_acp_case(
        spec,
        state_root=state_root,
        cwd=cwd,
        dispatch_id=f"matrix-{spec.label}-roundtrip",
        prompt=ROUNDTRIP_PROMPT,
        timeout_s=spec.timeout_s,
        idle_timeout_s=min(300.0, spec.timeout_s),
    )
    deferred = _claude_deferred(spec, run)
    if deferred:
        return MatrixCell(spec.label, "round_trip", "SKIP", deferred, run.dispatch_id)
    if not _is_complete(run):
        detail = f"state={_status_from_payload(run.payload)} rc={run.returncode}"
        if run.timed_out:
            detail += f" timeout>{round(run.elapsed_s, 1)}s"
        return MatrixCell(spec.label, "round_trip", "FAIL", detail, run.dispatch_id)
    markers = (run.payload or {}).get("markers") or {}
    if not markers.get("COMPLETE"):
        return MatrixCell(spec.label, "round_trip", "FAIL", "complete state without COMPLETE marker", run.dispatch_id)
    return MatrixCell(spec.label, "round_trip", "PASS", f"complete in {round(run.elapsed_s, 1)}s", run.dispatch_id)


def _router_decisions(payload: dict[str, Any] | None, decision: str | None = None) -> list[dict[str, Any]]:
    rows = list((payload or {}).get("permission_router_decisions") or [])
    if decision is None:
        return rows
    return [row for row in rows if row.get("decision") == decision]


def _has_locations(payload: dict[str, Any] | None) -> bool:
    for row in _router_decisions(payload):
        if row.get("locations"):
            return True
    for call in (payload or {}).get("tool_calls") or []:
        if call.get("locations"):
            return True
    return False


def _auto_and_locations(spec: AgentSpec, state_root: Path, work_root: Path) -> tuple[MatrixCell, MatrixCell]:
    base = work_root / spec.label / "permissions"
    in_cwd = base / "cwd"
    in_cwd.mkdir(parents=True, exist_ok=True)
    target = in_cwd / "gf_matrix_in_cwd.txt"
    write_prompt = (
        f"Create or update the file {target.name} in the current directory with exactly this text: "
        "GF-MATRIX-IN-CWD. Use the normal file editing tool. Write nowhere else. "
        "Then reply exactly on its own line: COMPLETE: GF-MATRIX-IN-CWD"
    )
    write_run = _run_acp_case(
        spec,
        state_root=state_root,
        cwd=in_cwd,
        dispatch_id=f"matrix-{spec.label}-in-cwd",
        prompt=write_prompt,
        timeout_s=spec.timeout_s,
        idle_timeout_s=min(300.0, spec.timeout_s),
    )
    deferred = _claude_deferred(spec, write_run)
    if deferred:
        return (
            MatrixCell(spec.label, "auto_permission", "SKIP", deferred, write_run.dispatch_id),
            MatrixCell(spec.label, "locations", "SKIP", deferred, write_run.dispatch_id),
        )
    allow_rows = _router_decisions(write_run.payload, "allow")
    file_ok = target.exists() and target.read_text(errors="replace").strip() == "GF-MATRIX-IN-CWD"
    in_cwd_ok = _is_complete(write_run) and bool(allow_rows) and file_ok and not (write_run.payload or {}).get("permission_pending")

    outside = base / "outside" / "gf_matrix_outside.txt"
    outside.parent.mkdir(parents=True, exist_ok=True)
    boundary_prompt = (
        f"Use the file editing tool to create the absolute path {outside} with text GF-MATRIX-OUTSIDE. "
        "This path is intentionally outside the current directory. Do not choose a different path."
    )
    boundary_run = _run_acp_case(
        spec,
        state_root=state_root,
        cwd=in_cwd,
        dispatch_id=f"matrix-{spec.label}-boundary",
        prompt=boundary_prompt,
        timeout_s=spec.timeout_s,
        idle_timeout_s=min(300.0, spec.timeout_s),
    )
    deferred = _claude_deferred(spec, boundary_run)
    if deferred:
        return (
            MatrixCell(spec.label, "auto_permission", "SKIP", deferred, boundary_run.dispatch_id),
            MatrixCell(spec.label, "locations", "SKIP", deferred, write_run.dispatch_id),
        )
    escalate_rows = _router_decisions(boundary_run.payload, "escalate")
    boundary_state = _status_from_payload(boundary_run.payload)
    boundary_ok = (
        bool(escalate_rows)
        and (boundary_run.payload or {}).get("permission_pending")
        and boundary_state in {"blocked", "blocked_permission_denied"}
        and not outside.exists()
    )

    if in_cwd_ok and boundary_ok:
        auto = MatrixCell(
            spec.label,
            "auto_permission",
            "PASS",
            f"allow={len(allow_rows)} escalate={len(escalate_rows)}",
            boundary_run.dispatch_id,
        )
    else:
        auto = MatrixCell(
            spec.label,
            "auto_permission",
            "FAIL",
            (
                f"in_cwd_ok={in_cwd_ok} file_ok={file_ok} allow={len(allow_rows)} "
                f"boundary_state={boundary_state} escalate={len(escalate_rows)} outside_created={outside.exists()}"
            ),
            boundary_run.dispatch_id,
        )

    locations_ok = _has_locations(write_run.payload)
    locations = MatrixCell(
        spec.label,
        "locations",
        "PASS" if locations_ok else "FAIL",
        "write tool-call carried locations" if locations_ok else "no locations surfaced for in-cwd write",
        write_run.dispatch_id,
    )
    return auto, locations


def _held_permission(spec: AgentSpec, state_root: Path, work_root: Path, hold_s: float) -> MatrixCell:
    base = work_root / spec.label / "held"
    cwd = base / "cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    outside = base / "outside" / "gf_matrix_held.txt"
    outside.parent.mkdir(parents=True, exist_ok=True)
    prompt = (
        f"Use the file editing tool to create the absolute path {outside} with text GF-MATRIX-HELD. "
        "This path is intentionally outside the current directory. Do not choose a different path."
    )
    run = _run_acp_case(
        spec,
        state_root=state_root,
        cwd=cwd,
        dispatch_id=f"matrix-{spec.label}-held",
        prompt=prompt,
        timeout_s=max(spec.timeout_s, hold_s + 180.0),
        permission_mode="inline",
        inline_timeout_s=hold_s,
        heartbeat_interval_s=hold_s + 5.0,
        idle_timeout_s=hold_s + 120.0,
        max_tool_s=hold_s + 180.0,
    )
    deferred = _claude_deferred(spec, run)
    if deferred:
        return MatrixCell(spec.label, "held_permission", "SKIP", deferred, run.dispatch_id)
    state = _status_from_payload(run.payload)
    auto_declined = list((run.payload or {}).get("permission_auto_declined") or [])
    bad_states = {"wedged", "tool_timeout", "remote_turn_silence"}
    ok = (
        state not in bad_states
        and bool(auto_declined)
        and run.elapsed_s >= min(60.0, hold_s)
        and not outside.exists()
    )
    detail = (
        f"state={state} elapsed={round(run.elapsed_s, 1)}s "
        f"auto_declined={len(auto_declined)} outside_created={outside.exists()}"
    )
    return MatrixCell(spec.label, "held_permission", "PASS" if ok else "FAIL", detail, run.dispatch_id)


def _ledger_stats(spec: AgentSpec, state_root: Path) -> MatrixCell:
    env = _matrix_env(state_root)
    status_proc = subprocess.run(
        [sys.executable, str(STATUS), "--json"],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    stats_proc = subprocess.run(
        [sys.executable, str(DISPATCH), "--stats", "1h", "--json"],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    records = []
    try:
        records = (json.loads(status_proc.stdout).get("dispatch") or {}).get("records") or []
    except json.JSONDecodeError:
        records = []
    try:
        stats = json.loads(stats_proc.stdout)
    except json.JSONDecodeError:
        stats = {}
    agent_records = [row for row in records if row.get("agent") == spec.runner_agent]
    stats_total = ((stats.get("by_shape") or {}).get("acp") or {}).get("total", 0)
    ok = status_proc.returncode == 0 and stats_proc.returncode == 0 and bool(agent_records) and stats_total >= 1
    detail = f"records={len(agent_records)} stats_acp_total={stats_total}"
    return MatrixCell(spec.label, "ledger_stats", "PASS" if ok else "FAIL", detail)


def _silent_turn(spec: AgentSpec, state_root: Path) -> MatrixCell:
    profile, remote_turn_silence_s = adapter_liveness_config(spec.runner_agent)
    ok = True
    detail = f"profile={profile} remote_turn_silence_s={remote_turn_silence_s}"
    if profile == "remote_api":
        ok = remote_turn_silence_s >= 1200.0
        detail += " (>=20m tolerance armed)" if ok else " (<20m tolerance)"
    elif profile not in {"local_compute", "hybrid"}:
        ok = False
    return MatrixCell(spec.label, "silent_turn", "PASS" if ok else "FAIL", detail)


def _skip_all(spec: AgentSpec, reason: str) -> list[MatrixCell]:
    status = "SKIP" if reason.startswith("SKIP") else "FAIL"
    return [MatrixCell(spec.label, prop, status, reason) for prop in PROPERTIES]


def _skip_remaining(
    spec: AgentSpec,
    reason: str,
    emitted_properties: set[str],
) -> list[MatrixCell]:
    return [
        MatrixCell(spec.label, prop, "SKIP", reason)
        for prop in PROPERTIES
        if prop not in emitted_properties
    ]


def run_matrix(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    selected = set(args.agents or [spec.label for spec in AGENTS])
    specs = [spec for spec in AGENTS if spec.label in selected]
    state_root = Path(args.state_dir or tempfile.mkdtemp(prefix="goalflight-acp-matrix-")).resolve()
    work_root = state_root / "work"
    work_root.mkdir(parents=True, exist_ok=True)
    cells: list[MatrixCell] = []

    if goalflight_compat.is_windows():
        for spec in specs:
            cells.extend(_skip_all(spec, "SKIP-unavailable: native Windows dispatch unsupported"))
    else:
        for spec in specs:
            ready, reason = _preflight(spec)
            if not ready:
                cells.extend(_skip_all(spec, reason))
                continue
            round_cell = _round_trip(spec, state_root, work_root)
            cells.append(round_cell)
            if round_cell.status == "SKIP":
                for prop in PROPERTIES:
                    if prop != "round_trip":
                        cells.append(MatrixCell(spec.label, prop, "SKIP", round_cell.detail))
                continue
            if round_cell.status == "FAIL":
                for prop in PROPERTIES:
                    if prop != "round_trip":
                        cells.append(MatrixCell(spec.label, prop, "SKIP", "round_trip failed"))
                continue
            auto, locations = _auto_and_locations(spec, state_root, work_root)
            cells.append(auto)
            if _is_claude_defer_cell(spec, auto):
                cells.extend(_skip_remaining(spec, auto.detail, {"round_trip", "auto_permission"}))
                continue
            cells.append(_ledger_stats(spec, state_root))
            held = _held_permission(spec, state_root, work_root, args.hold_seconds)
            cells.append(held)
            if _is_claude_defer_cell(spec, held):
                cells.extend(
                    _skip_remaining(
                        spec,
                        held.detail,
                        {"round_trip", "auto_permission", "ledger_stats", "held_permission"},
                    )
                )
                continue
            cells.append(locations)
            if _is_claude_defer_cell(spec, locations):
                cells.extend(
                    _skip_remaining(
                        spec,
                        locations.detail,
                        {"round_trip", "auto_permission", "ledger_stats", "held_permission", "locations"},
                    )
                )
                continue
            cells.append(_silent_turn(spec, state_root))

    report = {
        "schema": "goalflight.acp-push-gate-matrix.v1",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "state_root": str(state_root),
        "gate": "GOALFLIGHT_ACP_LIVE_MATRIX",
        "hold_seconds": args.hold_seconds,
        "agents": [asdict(spec) for spec in specs],
        "properties": list(PROPERTIES),
        "results": [asdict(cell) for cell in cells],
    }
    failed = [cell for cell in cells if cell.status == "FAIL"]
    return (1 if failed else 0), report


def _print_matrix(report: dict[str, Any]) -> None:
    cells = report["results"]
    by_agent = {agent["label"]: {prop: "SKIP" for prop in PROPERTIES} for agent in report["agents"]}
    for cell in cells:
        by_agent.setdefault(cell["agent"], {})[cell["property"]] = cell["status"]
    print("| agent | " + " | ".join(PROPERTIES) + " |")
    print("|" + "|".join(["---"] * (len(PROPERTIES) + 1)) + "|")
    for agent in by_agent:
        row = [agent] + [by_agent[agent].get(prop, "SKIP") for prop in PROPERTIES]
        print("| " + " | ".join(row) + " |")
    for cell in cells:
        print(f"{cell['status']}: {cell['agent']} {cell['property']} - {cell['detail']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the gated live ACP push-gate matrix.")
    parser.add_argument("--agents", nargs="*", choices=[spec.label for spec in AGENTS])
    parser.add_argument("--state-dir", help="Isolated state root for matrix ledger/status files.")
    parser.add_argument("--report", help="JSON report path.")
    parser.add_argument(
        "--hold-seconds",
        type=float,
        default=float(os.environ.get("GOALFLIGHT_ACP_LIVE_MATRIX_HOLD_S", "65")),
        help="Inline permission hold duration. Must stay >60 for the push-gate property.",
    )
    args = parser.parse_args(argv)

    if os.environ.get("GOALFLIGHT_ACP_LIVE_MATRIX") != "1":
        print("SKIP: set GOALFLIGHT_ACP_LIVE_MATRIX=1 to run real ACP push-gate matrix")
        return 0
    if args.hold_seconds <= 60:
        print("FAIL: --hold-seconds must be >60 for held-permission tolerance")
        return 1

    rc, report = run_matrix(args)
    _print_matrix(report)
    report_path = Path(
        args.report
        or ROOT / "docs-private" / "reports" / f"acp-push-gate-matrix-{_now_slug()}.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"report: {report_path}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
