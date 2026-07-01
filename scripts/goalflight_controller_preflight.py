#!/usr/bin/env python3
"""Orchestrator host preflight matrix (Track A goal 6 — Phase 0 MVP)."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import goalflight_compat  # noqa: E402

DEFAULT_CONTEXT_ORDER = ("AGENTS.md", "SKILL.md")
DEFAULT_STEERING_OPS = ("show", "propose", "apply", "explain")

CONTROLLER_STATUS = {
    "cursor": "green_required",
    "claude": "green_required",
    "codex": "yellow_until_green",
    "opencode": "probe_only",
    "grok": "controller_red",
}


def default_fleet_dir() -> Path:
    return goalflight_compat.resolve_env_path(
        "GOALFLIGHT_FLEET_DIR", Path.home() / ".goal-flight" / "fleet"
    )


def load_adapter(repo_root: Path, adapter: str) -> dict | None:
    path = repo_root / "adapters" / f"{adapter}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def controller_contract(adapter_doc: dict | None) -> dict:
    if not adapter_doc:
        return {}
    return adapter_doc.get("controller_contract") or {}


def check_context_files(repo_root: Path, order: tuple[str, ...]) -> list[dict]:
    checks: list[dict] = []
    for rel in order:
        path = repo_root / rel
        checks.append({"path": rel, "exists": path.exists(), "ok": path.exists()})
    return checks


def run_cmd(cmd: list[str], *, cwd: Path | None = None, timeout: float = 12.0) -> dict:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return {
            "cmd": cmd,
            "returncode": proc.returncode,
            "ok": proc.returncode == 0,
            "stdout": (proc.stdout or "").strip()[:500],
            "stderr": (proc.stderr or "").strip()[:500],
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"cmd": cmd, "returncode": None, "ok": False, "stderr": str(exc)}


def check_steering_ops(fleet_dir: Path, ops: tuple[str, ...]) -> list[dict]:
    python = goalflight_compat.python_executable()
    checks: list[dict] = []
    for op in ops:
        if op == "show":
            cmd = [python, str(SCRIPT_DIR / "goalflight_fleet.py"), "steering", "show"]
        elif op == "propose":
            cmd = [
                python,
                str(SCRIPT_DIR / "goalflight_fleet.py"),
                "steering",
                "propose",
                "--node",
                "preflight-probe",
            ]
        else:
            cmd = [python, str(SCRIPT_DIR / "goalflight_fleet.py"), "steering", op, "--help"]
        result = run_cmd(cmd)
        if op == "propose" and result["ok"]:
            # noop proposals are ok; missing fleet dir is not
            result["ok"] = "noop" in result["stdout"] or "proposal_id" in result["stdout"]
        checks.append({"op": op, **result})
    return checks


def check_router(repo_root: Path) -> dict:
    python = goalflight_compat.python_executable()
    validate = run_cmd([python, str(SCRIPT_DIR / "goalflight_actions.py"), "validate"], cwd=repo_root)
    route = run_cmd(
        [python, str(SCRIPT_DIR / "goalflight_actions.py"), "route", "core", "doctor", "read"],
        cwd=repo_root,
    )
    return {"validate": validate, "doctor_route": route, "ok": validate["ok"] and route["ok"]}


def check_binary(binary: str, *args: str) -> dict:
    path = shutil.which(binary)
    if not path:
        return {"present": False, "ok": False}
    result = run_cmd([path, *args])
    return {"present": True, "path": path, "ok": result["ok"], "detail": result.get("stdout") or result.get("stderr")}


def evaluate_status(adapter: str, checks: dict) -> str:
    policy = CONTROLLER_STATUS.get(adapter, "probe_only")
    if policy == "controller_red":
        return "red"
    context_ok = all(item.get("ok") for item in checks.get("context_files") or [])
    router_ok = checks.get("router", {}).get("ok", False)
    steering_ok = all(item.get("ok") for item in checks.get("steering_ops") or [])
    cli_ok = checks.get("host_cli", {}).get("ok", False)
    core_ok = context_ok and router_ok and steering_ok
    if policy == "green_required":
        return "green" if core_ok and cli_ok else "red"
    if policy == "yellow_until_green":
        if core_ok and cli_ok:
            return "green"
        if core_ok:
            return "yellow"
        return "red"
    if core_ok:
        return "yellow" if not cli_ok else "green"
    return "red"


def remediation(adapter: str, status: str, checks: dict) -> list[str]:
    hints: list[str] = []
    if status == "red" and CONTROLLER_STATUS.get(adapter) == "controller_red":
        hints.append("Grok is worker-only in Phase 0; do not promote to orchestrator.")
    if not all(item.get("ok") for item in checks.get("context_files") or []):
        hints.append("Restore AGENTS.md and SKILL.md in project root.")
    if not checks.get("router", {}).get("ok"):
        hints.append("Run: GOALFLIGHT_PYTHON=<python> scripts/goalflight_actions.py validate")
    host_cli = checks.get("host_cli") or {}
    if not host_cli.get("present"):
        hints.append(f"Install {adapter} CLI and re-run preflight.")
    elif not host_cli.get("ok"):
        hints.append(f"Fix {adapter} CLI auth/version probe.")
    if status == "yellow" and adapter == "codex":
        hints.append("Codex orchestrator may run yellow until run-context bundle is generated.")
    return hints


def preflight(
    repo_root: Path,
    adapter: str,
    *,
    fleet_dir: Path | None = None,
) -> dict:
    fleet_dir = fleet_dir or default_fleet_dir()
    if not fleet_dir.exists():
        import goalflight_fleet as fleet

        fleet.bootstrap(fleet_dir)
    adapter_doc = load_adapter(repo_root, adapter)
    contract = controller_contract(adapter_doc)
    context_order = tuple(contract.get("context_load_order") or DEFAULT_CONTEXT_ORDER)
    steering_ops = tuple(contract.get("fleet_steering_ops") or DEFAULT_STEERING_OPS)

    host_binary = {
        "cursor": "cursor-agent",
        "claude": "claude",
        "codex": "codex",
        "grok": "grok",
        "opencode": "opencode",
    }.get(adapter, adapter)

    checks = {
        "context_files": check_context_files(repo_root, context_order),
        "router": check_router(repo_root),
        "steering_ops": check_steering_ops(fleet_dir, steering_ops),
        "host_cli": check_binary(host_binary, "--version"),
    }
    status = evaluate_status(adapter, checks)
    payload = {
        "schema": "goalflight.controller-preflight.v1",
        "adapter": adapter,
        "repo_root": str(repo_root),
        "status": status,
        "policy": CONTROLLER_STATUS.get(adapter, "probe_only"),
        "checks": checks,
        "remediation": remediation(adapter, status, checks),
    }
    return payload


def write_probe_artifact(fleet_dir: Path, adapter: str, payload: dict) -> Path:
    probes_dir = fleet_dir / "probes"
    probes_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = probes_dir / f"controller-{adapter}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Orchestrator host preflight matrix")
    parser.add_argument("--adapter", required=True, choices=sorted(CONTROLLER_STATUS))
    parser.add_argument("--project-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--fleet-dir", type=Path, default=default_fleet_dir())
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-write-probe", action="store_true")
    args = parser.parse_args(argv)
    payload = preflight(args.project_root.resolve(), args.adapter, fleet_dir=args.fleet_dir)
    if not args.no_write_probe:
        payload["probe_path"] = str(write_probe_artifact(args.fleet_dir, args.adapter, payload))
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"adapter={args.adapter} status={payload['status']}")
        for hint in payload.get("remediation") or []:
            print(f"  - {hint}")
    status = payload["status"]
    policy = payload["policy"]
    if policy == "controller_red":
        return 2
    if status == "green":
        return 0
    if status == "yellow" and policy == "yellow_until_green":
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
