#!/usr/bin/env python3
"""ACP hardening matrix — wedges, timeouts, auto-mode, os-sandbox, permission windows.

Not part of ./tests/run.sh. Hermetic fakes always run. Live workers need
GOALFLIGHT_LIVE_ACP=1. Fleet dispatch needs GOALFLIGHT_LIVE_SSH=1 and
GOALFLIGHT_LIVE_FLEET=1.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT / "tests/python"))
sys.path.insert(0, str(ROOT / "tests/manual"))
sys.path.insert(0, str(SCRIPTS))

from test_acp_failure_modes import (  # noqa: E402
    _make_fake_agent_wrapper,
    _run_fake_runner,
    _write_supported_adapter_manifest,
    case_handshake_wedge_kills_before_respawn,
)
from test_acp_prompt_matrix import (  # noqa: E402
    SHORT_PROMPT,
    discover_live_agents,
    parse_acp_json,
    record,
    run_cmd,
)

AUTO_PROMPT = (
    "Auto-mode hardening: reply with exactly GF-AUTO-OK. "
    "Do not use tools or ask for permission."
)

# Agent-side ACP failures tracked separately from orchestrator regressions.
KNOWN_LIVE_AGENT_ISSUES = {
    "claude": "ACP initialize/session failure on trivial prompts (vendor -32603)",
}


def _fleet_reconcile_stale() -> None:
    fleet_dir = os.environ.get("GOALFLIGHT_FLEET_DIR", str(Path.home() / ".goal-flight/fleet"))
    run_cmd(
        [
            sys.executable,
            str(SCRIPTS / "goalflight_fleet.py"),
            "--fleet-dir",
            fleet_dir,
            "reconcile",
            "--release-stale",
        ],
        timeout=120,
    )


def _write_fake_manifest_with_sandbox(adapters_dir: Path, name: str) -> None:
    _write_supported_adapter_manifest(adapters_dir, name)
    path = adapters_dir / f"{name}.json"
    manifest = json.loads(path.read_text())
    manifest.setdefault("permission_surface", {})["os_sandbox"] = {
        "supported_profiles": ["off", "read-only", "workspace-write"],
        "default_profile": "off",
        "implementation": "runner:sandbox-exec",
    }
    path.write_text(json.dumps(manifest))


def run_fake(
    scenario: str,
    *,
    expect_state: str,
    expect_ok: bool | None = None,
    progress_stall_s: float = 30.0,
    idle_timeout: float = 10.0,
    max_tool_s: float = 10.0,
    timeout_s: float = 20.0,
    os_sandbox: str | None = None,
    permission_mode: str = "auto",
    permission_inline_timeout_s: float | None = None,
    extra_env: dict[str, str] | None = None,
) -> tuple[bool, str, dict | None]:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        status = tmp / "status.json"
        wrapper = _make_fake_agent_wrapper(tmp, scenario=scenario)
        adapters_dir = tmp / "adapters"
        _write_fake_manifest_with_sandbox(adapters_dir, wrapper.name)
        env = os.environ.copy()
        env.update(
            {
                "GOALFLIGHT_STATE_DIR": str(state_dir),
                "GOALFLIGHT_FAKE_ACP_SCENARIO": scenario,
                "GOALFLIGHT_FAKE_ACP_INTERVAL": "0.05",
                "GOALFLIGHT_ACP_PYTHON": sys.executable,
                "GOALFLIGHT_ADAPTERS_DIR": str(adapters_dir),
            }
        )
        if extra_env:
            env.update(extra_env)
        argv = [
            sys.executable,
            str(SCRIPTS / "goalflight_acp_run.py"),
            "--agent",
            str(wrapper),
            "--cwd",
            str(ROOT),
            "--prompt-text",
            "hardening",
            "--status-json",
            str(status),
            "--progress-stall-s",
            str(progress_stall_s),
            "--idle-timeout",
            str(idle_timeout),
            "--max-tool-s",
            str(max_tool_s),
            "--permission-mode",
            permission_mode,
            "--json",
        ]
        if permission_inline_timeout_s is not None:
            argv.extend(["--permission-inline-timeout-s", str(permission_inline_timeout_s)])
        if os_sandbox:
            argv.extend(["--os-sandbox", os_sandbox])
        proc = run_cmd(argv, env=env, timeout=timeout_s)
        payload = json.loads(status.read_text()) if status.exists() else parse_acp_json(proc.stdout)
    if payload is None:
        tail = (proc.stderr or proc.stdout or "")[-400:]
        return False, f"no status rc={proc.returncode} {tail}", None
    state = payload.get("state")
    ok = state == expect_state
    if expect_ok is not None and payload.get("ok") is not expect_ok:
        ok = False
    detail = f"state={state} ok={payload.get('ok')}"
    if payload.get("error"):
        detail += f" err={payload['error']}"
    return ok, detail, payload


def live_run(
    agent: str,
    prompt: str,
    *,
    timeout: float = 240,
    os_sandbox: str | None = None,
    permission_mode: str = "auto",
) -> tuple[bool, str, dict | None]:
    with tempfile.TemporaryDirectory() as td:
        status = Path(td) / "status.json"
        argv = [
            sys.executable,
            str(SCRIPTS / "goalflight_acp_run.py"),
            "--agent",
            agent,
            "--cwd",
            str(ROOT),
            "--prompt-text",
            prompt,
            "--status-json",
            str(status),
            "--permission-mode",
            permission_mode,
            "--idle-timeout",
            str(min(600, max(120, int(timeout - 30)))),
            "--json",
        ]
        if os_sandbox:
            argv.extend(["--os-sandbox", os_sandbox])
        proc = run_cmd(argv, timeout=timeout)
    payload = parse_acp_json(proc.stdout)
    if payload is None and status.exists():
        payload = json.loads(status.read_text())
    if payload is None:
        tail = (proc.stderr or proc.stdout or "")[-500:]
        return False, f"exit={proc.returncode} {tail}", None
    state = payload.get("state")
    ok = proc.returncode == 0 and state == "complete" and payload.get("ok") is True
    detail = f"state={state} events={payload.get('events_seen')}"
    if payload.get("permission_auto_declined"):
        detail += f" auto_declined={len(payload['permission_auto_declined'])}"
    return ok, detail, payload


def os_sandbox_supported(agent: str) -> bool:
    from goalflight_adapter_readiness import load_manifest

    manifest = load_manifest(agent)
    if not manifest:
        return False
    profiles = (
        manifest.get("permission_surface", {})
        .get("os_sandbox", {})
        .get("supported_profiles", [])
    )
    return "read-only" in profiles


def run_hermetic(results: list[dict]) -> None:
    print("== hermetic wedges / timeouts ==")
    cases = [
        ("wedge/progress-then-silent", lambda: _run_fake_runner("progress_then_silent", progress_stall_s=30.0)),
        ("timeout/idle-silent", lambda: _run_fake_runner("idle_silent", progress_stall_s=30.0, idle_timeout=0.5, timeout_s=20.0)),
        ("timeout/tool-stuck", lambda: _run_fake_runner("tool_stuck", progress_stall_s=30.0, max_tool_s=0.5, timeout_s=20.0)),
        ("timeout/progress-stall-vendor", lambda: _run_fake_runner("raw_vendor_flood", progress_stall_s=0.5, timeout_s=20.0)),
    ]
    expected = {
        "wedge/progress-then-silent": ("wedged", "wedged_by_heartbeat"),
        "timeout/idle-silent": ("failed", "agent_timeout (idle)"),
        "timeout/tool-stuck": ("tool_timeout", "tool_timeout"),
        "timeout/progress-stall-vendor": ("wedged", "progress_stall"),
    }
    for label, fn in cases:
        try:
            rc, status, _stdout, _stderr = fn()
            exp_state, exp_msg = expected[label]
            ok = status.get("state") == exp_state and (status.get("error") or {}).get("message") == exp_msg
            record(results, layer="hermetic", case=label, ok=ok, detail=f"state={status.get('state')} rc={rc}")
        except Exception as e:
            record(results, layer="hermetic", case=label, ok=False, detail=str(e))

    print("== hermetic handshake wedge ==")
    try:
        case_handshake_wedge_kills_before_respawn()
        record(results, layer="hermetic", case="wedge/handshake-retry-kills", ok=True)
    except Exception as e:
        record(results, layer="hermetic", case="wedge/handshake-retry-kills", ok=False, detail=str(e))

    print("== hermetic auto-mode permissions ==")
    for label, scenario in (
        ("perm/auto-codex-shape", "permission_codex"),
        ("perm/auto-elicitation", "permission_elicitation"),
        ("perm/auto-reject-only", "permission_reject_only"),
    ):
        ok, detail, payload = run_fake(scenario, expect_state="complete", expect_ok=True, timeout_s=25.0)
        record(results, layer="hermetic", case=label, ok=ok, detail=detail)

    print("== hermetic inline permission timeout ==")
    ok, detail, payload = run_fake(
        "permission_inline",
        expect_state="complete",
        expect_ok=True,
        permission_mode="inline",
        permission_inline_timeout_s=0.35,
        timeout_s=25.0,
        extra_env={"GOALFLIGHT_ACP_PYTHON": sys.executable},
    )
    if payload:
        excerpt = (payload.get("text_excerpt") or "")[:80]
        if "permission:cancelled" not in excerpt and "permission:" not in excerpt:
            ok = False
            detail += f" excerpt={excerpt!r}"
    record(results, layer="hermetic", case="perm/inline-timeout-auto-decline", ok=ok, detail=detail)

    if platform.system() == "Darwin":
        print("== hermetic os-sandbox (fake echo) ==")
        ok, detail, _ = run_fake(
            "echo",
            expect_state="complete",
            expect_ok=True,
            os_sandbox="read-only",
            timeout_s=25.0,
        )
        record(results, layer="hermetic", case="sandbox/fake-read-only-echo", ok=ok, detail=detail)
    else:
        print("SKIP hermetic os-sandbox (non-Darwin)")


def run_live(results: list[dict]) -> None:
    if os.environ.get("GOALFLIGHT_LIVE_ACP", "1") != "1":
        print("SKIP live (GOALFLIGHT_LIVE_ACP!=1)")
        return
    agents = discover_live_agents()
    print("== live auto-mode workers ==")
    print("agents:", ", ".join(agents) or "(none)")
    for agent in agents:
        t0 = time.time()
        ok, detail, _ = live_run(agent, AUTO_PROMPT, timeout=300, permission_mode="auto")
        elapsed = round(time.time() - t0, 1)
        known = KNOWN_LIVE_AGENT_ISSUES.get(agent)
        if known and not ok:
            ok = True
            detail = f"{detail} ({elapsed}s) KNOWN: {known}"
        else:
            detail = f"{detail} ({elapsed}s)"
        record(results, layer="live", case=f"auto/{agent}", ok=ok, detail=detail)
        if os_sandbox_supported(agent) and platform.system() == "Darwin":
            t0 = time.time()
            ok2, detail2, _ = live_run(
                agent,
                AUTO_PROMPT,
                timeout=300,
                permission_mode="auto",
                os_sandbox="read-only",
            )
            elapsed2 = round(time.time() - t0, 1)
            record(
                results,
                layer="live",
                case=f"sandbox-readonly/{agent}",
                ok=ok2,
                detail=f"{detail2} ({elapsed2}s)",
            )


def run_fleet_dispatch(
    node: str,
    agent: str,
    prompt: str,
    *,
    label: str,
    results: list[dict],
    billing: str | None = None,
) -> None:
    fleet_dir = os.environ.get("GOALFLIGHT_FLEET_DIR", str(Path.home() / ".goal-flight/fleet"))
    account = billing or os.environ.get("GOALFLIGHT_FLEET_BILLING", "openai/default")
    proc = run_cmd(
        [
            sys.executable,
            str(SCRIPTS / "goalflight_fleet.py"),
            "--fleet-dir",
            fleet_dir,
            "dispatch",
            "--node",
            node,
            "--agent",
            agent,
            "--billing-account",
            account,
            "--prompt",
            prompt,
            "--thin-defaults",
            "--exec",
            "--json",
        ],
        timeout=900,
    )
    payload = parse_acp_json(proc.stdout)
    ok = proc.returncode == 0 and payload and payload.get("ok") is True
    finalize = (payload or {}).get("finalize") or {}
    detail = f"exit={proc.returncode}"
    if payload:
        detail += f" dispatch={payload.get('dispatch_id')}"
        if finalize:
            detail += f" finalize={finalize.get('ok')} released={(finalize.get('reconcile') or {}).get('released')}"
    if not ok:
        detail += " " + (proc.stderr or "")[-300:]
    record(results, layer="fleet", case=label, ok=ok, detail=detail.strip())


def run_fleet(results: list[dict]) -> None:
    if os.environ.get("GOALFLIGHT_LIVE_FLEET", "1") != "1" or os.environ.get("GOALFLIGHT_LIVE_SSH") != "1":
        print("SKIP fleet (need GOALFLIGHT_LIVE_SSH=1 and GOALFLIGHT_LIVE_FLEET=1)")
        return
    _fleet_reconcile_stale()
    node = os.environ.get("GOALFLIGHT_FLEET_NODE", "mac-studio-256-1")
    print(f"== fleet auto-mode ({node}) ==")
    fleet_cases = [
        ("codex-acp", "openai/default", f"fleet/{node}/auto-codex"),
        ("grok", "grok/shared", f"fleet/{node}/auto-grok"),
    ]
    if os.environ.get("GOALFLIGHT_FLEET_OPENCODE", "") == "1":
        fleet_cases.append(("opencode", "openai/default", f"fleet/{node}/auto-opencode"))
    for agent, billing, label in fleet_cases:
        run_fleet_dispatch(node, agent, AUTO_PROMPT, label=label, results=results, billing=billing)


def main() -> int:
    results: list[dict] = []
    run_hermetic(results)
    run_live(results)
    run_fleet(results)

    passed = sum(1 for r in results if r["ok"])
    failed = [r for r in results if not r["ok"]]
    known_notes = [r for r in results if "KNOWN:" in r.get("detail", "")]
    print("\n===== hardening summary =====")
    print(f"{passed}/{len(results)} passed, {len(failed)} failed")
    if known_notes:
        print(f"known agent issues (counted pass): {len(known_notes)}")
    for row in failed:
        print(f"  FAIL {row['layer']}/{row['case']}: {row.get('detail', '')}")

    report = ROOT / "tests/manual/.acp-hardening-report.json"
    report.write_text(
        json.dumps(
            {
                "ts": time.time(),
                "known_live_agent_issues": KNOWN_LIVE_AGENT_ISSUES,
                "results": results,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"report: {report}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
