#!/usr/bin/env python3
"""Remote bash-tail readiness probe (Phase 2 goal 15a, non-gating).

Records probe artifacts under ``~/.goal-flight/fleet/probes/`` for codex-bash-tail
and opencode-bash-tail. Probe-only — does not block dispatch.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

PROBE_SCHEMA = "goalflight.fleet.bash-tail-probe.v1"

ADAPTER_SCRIPTS: dict[str, str] = {
    "codex-bash-tail": "scripts/goalflight_acp_run.py",
    "opencode-bash-tail": "scripts/opencode_bash_tail.py",
}


def probe_artifact_path(fleet_dir: Path, node_id: str, adapter: str) -> Path:
    safe = adapter.replace("/", "-")
    return fleet_dir / "probes" / f"bash-tail-{node_id}-{safe}.json"


def run_adapter_probe(
    fleet_dir: Path,
    node_id: str,
    adapter: str,
    *,
    runner: Callable[[list[str]], tuple[int, str, str]] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    import goalflight_fleet_store as fleet
    import goalflight_fleet_ssh as fleet_ssh

    started = time.monotonic()
    fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
    node_entry = (fleet_doc.get("nodes") or {}).get(node_id)
    if not isinstance(node_entry, dict):
        return {
            "schema": PROBE_SCHEMA,
            "ok": False,
            "node_id": node_id,
            "adapter": adapter,
            "error": f"unknown node: {node_id}",
        }

    script_rel = ADAPTER_SCRIPTS.get(adapter)
    if not script_rel:
        return {
            "schema": PROBE_SCHEMA,
            "ok": False,
            "node_id": node_id,
            "adapter": adapter,
            "error": f"unsupported adapter: {adapter}",
        }

    repo_root = str(node_entry.get("repo_root") or "")
    host = fleet_ssh.host_from_node_entry(node_id, node_entry)
    checks: dict[str, Any] = {"script": script_rel, "ssh_echo": False, "script_exists": False}

    try:
        echo_remote = fleet_ssh.build_remote_command("probe_echo", repo_root=repo_root)
        echo_argv = fleet_ssh.build_ssh_command(host, echo_remote, command_class="probe_echo")
        echo_run = fleet_ssh.run_ssh(echo_argv, runner=runner, dry_run=dry_run)
        checks["ssh_echo"] = bool(echo_run.get("ok"))

        script_remote = fleet_ssh.build_remote_command(
            "probe_script_exists",
            repo_root=repo_root,
            script=script_rel,
        )
        script_argv = fleet_ssh.build_ssh_command(host, script_remote, command_class="probe_script_exists")
        script_run = fleet_ssh.run_ssh(script_argv, runner=runner, dry_run=dry_run)
        checks["script_exists"] = bool(script_run.get("ok"))
    except fleet_ssh.SshAllowlistError as exc:
        return {
            "schema": PROBE_SCHEMA,
            "ok": False,
            "node_id": node_id,
            "adapter": adapter,
            "error": str(exc),
            "checks": checks,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    ok = checks["ssh_echo"] and checks["script_exists"]
    payload = {
        "schema": PROBE_SCHEMA,
        "ok": ok,
        "node_id": node_id,
        "adapter": adapter,
        "marker_seen": checks["script_exists"],
        "checks": checks,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "probed_at": fleet.iso(),
    }
    path = probe_artifact_path(fleet_dir, node_id, adapter)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def run_node_probes(
    fleet_dir: Path,
    node_id: str,
    *,
    adapters: list[str] | None = None,
    runner: Callable[[list[str]], tuple[int, str, str]] | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    targets = adapters or list(ADAPTER_SCRIPTS.keys())
    return [
        run_adapter_probe(fleet_dir, node_id, adapter, runner=runner, dry_run=dry_run)
        for adapter in targets
    ]


def load_latest_probes(fleet_dir: Path, node_id: str) -> dict[str, dict[str, Any]]:
    probes_dir = fleet_dir / "probes"
    out: dict[str, dict[str, Any]] = {}
    if not probes_dir.is_dir():
        return out
    prefix = f"bash-tail-{node_id}-"
    for path in sorted(probes_dir.glob(f"{prefix}*.json")):
        try:
            doc = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        adapter = str(doc.get("adapter") or "")
        if adapter:
            out[adapter] = doc
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    import goalflight_fleet_store as fleet

    parser = argparse.ArgumentParser(description="Fleet bash-tail remote probe (non-gating)")
    parser.add_argument("--fleet-dir", type=Path, default=fleet.default_fleet_dir())
    parser.add_argument("--node", required=True)
    parser.add_argument("--adapter", action="append", help="Repeat for multiple adapters")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    fleet.bootstrap(args.fleet_dir)
    rows = run_node_probes(
        args.fleet_dir,
        args.node,
        adapters=args.adapter,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps({"probes": rows}, indent=2))
    else:
        for row in rows:
            status = "ok" if row.get("ok") else "fail"
            print(f"{row.get('adapter')}\t{status}\t{row.get('duration_ms')}ms")
    return 0 if all(row.get("ok") for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
