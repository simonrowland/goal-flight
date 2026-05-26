#!/usr/bin/env python3
"""Manual ACP prompt matrix — short/long/sentinel across workers and fleet.

Not part of ./tests/run.sh. Set GOALFLIGHT_LIVE_ACP=1 for live worker runs.
Set GOALFLIGHT_LIVE_FLEET=1 for mac-studio fleet dispatch (also needs GOALFLIGHT_LIVE_SSH=1).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
FAKE = ROOT / "test/fixtures/acp_fake_agent.py"
sys.path.insert(0, str(ROOT / "test"))
from test_acp_failure_modes import (  # noqa: E402
    _make_fake_agent_wrapper,
    _write_supported_adapter_manifest,
)

SHORT_PROMPT = "Reply with exactly: GF-SHORT-OK and nothing else."
LONG_PROMPT = (
    "Goal-flight long-prompt matrix test. Ignore all filler; answer only with the token.\n\n"
    + textwrap.fill("context " * 400, width=72)
    + "\n\nReply with exactly: GF-LONG-OK and nothing else."
)
SENTINEL_PROMPT = (
    "Summarize in one sentence, then emit these marker lines exactly:\n"
    "RESULT: matrix-ok\nBLOCKED: none\nCOMPLETE: done\n"
    "Do not use tools.\n"
)


def record(results: list[dict], *, layer: str, case: str, ok: bool, detail: str = "", **extra) -> None:
    row = {"layer": layer, "case": case, "ok": ok, "detail": detail, **extra}
    results.append(row)
    mark = "PASS" if ok else "FAIL"
    print(f"{mark} [{layer}] {case}" + (f" — {detail}" if detail else ""))


def run_cmd(argv: list[str], *, cwd: Path | None = None, env: dict | None = None, timeout: float = 180) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        argv,
        cwd=cwd or ROOT,
        env=merged,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def parse_acp_json(stdout: str) -> dict | None:
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return None


def hermetic_fake_run(
    scenario: str,
    prompt: str,
    *,
    timeout: float = 60,
) -> tuple[bool, str, dict | None]:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        status = tmp / "status.json"
        wrapper = _make_fake_agent_wrapper(tmp, scenario=scenario)
        adapters_dir = tmp / "adapters"
        _write_supported_adapter_manifest(adapters_dir, wrapper.name)
        proc = run_cmd(
            [
                sys.executable,
                str(SCRIPTS / "goalflight_acp_run.py"),
                "--agent",
                str(wrapper),
                "--cwd",
                str(ROOT),
                "--prompt-text",
                prompt,
                "--status-json",
                str(status),
                "--idle-timeout",
                "30",
                "--progress-stall-s",
                "30",
                "--json",
            ],
            env={
                "GOALFLIGHT_STATE_DIR": str(state_dir),
                "GOALFLIGHT_FAKE_ACP_SCENARIO": scenario,
                "GOALFLIGHT_FAKE_ACP_INTERVAL": "0.05",
                "GOALFLIGHT_ACP_PYTHON": sys.executable,
                "GOALFLIGHT_ADAPTERS_DIR": str(adapters_dir),
            },
            timeout=timeout,
        )
        if status.exists():
            payload = json.loads(status.read_text())
        else:
            payload = parse_acp_json(proc.stdout)
    if payload is None:
        err = (proc.stderr or proc.stdout or "")[-400:]
        return False, err, None
    state = payload.get("state")
    ok = state == "complete" and payload.get("ok") is True
    detail = f"state={state} rc={proc.returncode}"
    if payload.get("error"):
        detail += f" err={payload['error']}"
    return ok, detail, payload


def live_acp_run(agent: str, prompt: str, *, timeout: float) -> tuple[bool, str, dict | None]:
    with tempfile.TemporaryDirectory() as td:
        status = Path(td) / "status.json"
        proc = run_cmd(
            [
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
                "--idle-timeout",
                str(min(600, max(120, int(timeout - 30)))),
                "--json",
            ],
            timeout=timeout,
        )
    payload = parse_acp_json(proc.stdout)
    if payload is None:
        tail = (proc.stderr or proc.stdout or "")[-500:]
        return False, f"exit={proc.returncode} {tail}", None
    state = payload.get("state")
    ok = proc.returncode == 0 and state == "complete" and payload.get("ok") is True
    markers = payload.get("markers") or {}
    detail = f"state={state} events={payload.get('events_seen')}"
    if markers.get("BLOCKED"):
        detail += f" BLOCKED={markers['BLOCKED'][-1]!r}"
    return ok, detail, payload


def discover_live_agents() -> list[str]:
    sys.path.insert(0, str(SCRIPTS))
    from goalflight_adapter_readiness import load_manifest, validate_acp_dispatch_readiness
    import goalflight_acp_run as acp_run_mod

    candidates = [
        "codex-acp",
        "claude",
        "grok",
        "opencode",
        "cursor-agent",
    ]
    ready: list[str] = []
    for agent in candidates:
        if load_manifest(agent) is None:
            continue
        argv = list(acp_run_mod.agent_command(agent))
        if validate_acp_dispatch_readiness(agent, argv) is None:
            ready.append(agent)
    return ready


def run_fleet_exec(node: str, prompt: str, *, label: str, results: list[dict]) -> None:
    fleet_dir = os.environ.get("GOALFLIGHT_FLEET_DIR", str(Path.home() / ".goal-flight/fleet"))
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
            "codex-acp",
            "--billing-account",
            os.environ.get("GOALFLIGHT_FLEET_BILLING", "openai/default"),
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


def main() -> int:
    live_acp = os.environ.get("GOALFLIGHT_LIVE_ACP", "1") == "1"
    live_fleet = os.environ.get("GOALFLIGHT_LIVE_FLEET", "1") == "1"
    results: list[dict] = []

    print("== hermetic fake-agent matrix ==")
    for scenario, prompt, label, expect in (
        ("echo", SHORT_PROMPT, "fake/short", "complete"),
        ("echo", LONG_PROMPT[:2000], "fake/long-trunc", "complete"),
        ("blocked_none", SENTINEL_PROMPT, "fake/blocked-none", "complete"),
        ("user_need_none", SENTINEL_PROMPT, "fake/user-need-none", "complete"),
        ("blocked", SHORT_PROMPT, "fake/blocked-substantive", "blocked"),
    ):
        ok, detail, payload = hermetic_fake_run(scenario, prompt)
        if payload is not None:
            ok = payload.get("state") == expect
        record(results, layer="hermetic", case=label, ok=ok, detail=detail)

    if live_acp:
        print("== live local ACP workers ==")
        agents = discover_live_agents()
        print("agents:", ", ".join(agents) or "(none)")
        for agent in agents:
            for kind, prompt, timeout in (
                ("short", SHORT_PROMPT, 240),
                ("long", LONG_PROMPT, 720),
                ("sentinel", SENTINEL_PROMPT, 300),
            ):
                t0 = time.time()
                ok, detail, _payload = live_acp_run(agent, prompt, timeout=timeout)
                elapsed = round(time.time() - t0, 1)
                record(
                    results,
                    layer="live",
                    case=f"{agent}/{kind}",
                    ok=ok,
                    detail=f"{detail} ({elapsed}s)",
                )
                if kind == "short" and not ok:
                    record(
                        results,
                        layer="live",
                        case=f"{agent}/long",
                        ok=False,
                        detail="skipped (short failed)",
                    )
                    record(
                        results,
                        layer="live",
                        case=f"{agent}/sentinel",
                        ok=False,
                        detail="skipped (short failed)",
                    )
                    break
    else:
        print("SKIP live ACP (GOALFLIGHT_LIVE_ACP!=1)")

    if live_fleet and os.environ.get("GOALFLIGHT_LIVE_SSH") == "1":
        print("== fleet live (mac-studio) ==")
        node = os.environ.get("GOALFLIGHT_FLEET_NODE", "mac-studio-256-1")
        run_fleet_exec(node, SHORT_PROMPT, label=f"fleet/{node}/short", results=results)
        run_fleet_exec(node, LONG_PROMPT[:1500], label=f"fleet/{node}/long", results=results)
        run_fleet_exec(node, SENTINEL_PROMPT, label=f"fleet/{node}/sentinel", results=results)
    else:
        print("SKIP fleet live (set GOALFLIGHT_LIVE_SSH=1 and GOALFLIGHT_LIVE_FLEET=1)")

    passed = sum(1 for r in results if r["ok"])
    failed = [r for r in results if not r["ok"]]
    print("\n===== summary =====")
    print(f"{passed}/{len(results)} passed, {len(failed)} failed")
    if failed:
        for row in failed:
            print(f"  FAIL {row['layer']}/{row['case']}: {row.get('detail','')}")

    report = ROOT / "test/manual/.prompt-matrix-report.json"
    report.write_text(json.dumps({"ts": time.time(), "results": results}, indent=2) + "\n")
    print(f"report: {report}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
