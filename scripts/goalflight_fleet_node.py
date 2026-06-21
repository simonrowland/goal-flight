#!/usr/bin/env python3
"""Interactive node onboarding for fleet store (Track A goal 7)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import goalflight_fleet_ssh as fleet_ssh


PROBE_PLAN: tuple[tuple[str, dict[str, Any]], ...] = (
    ("probe_echo", {}),
    ("probe_repo_exists", {}),
    ("probe_script_exists", {"script": "scripts/goalflight_doctor.py"}),
)


def failure_remediation_table() -> str:
    lines = ["Failure remediation:", ""]
    for code, hint in fleet_ssh.REMEDIATION_HINTS.items():
        lines.append(f"- {code}: {hint}")
    return "\n".join(lines)


def run_node_probes(
    host: fleet_ssh.SshHostSpec,
    *,
    repo_root: str,
    runner: Callable[[list[str]], tuple[int, str, str]] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for command_class, extra in PROBE_PLAN:
        remote = fleet_ssh.build_remote_command(command_class, repo_root=repo_root, **extra)
        ssh_argv = fleet_ssh.build_ssh_command(host, remote, command_class=command_class)
        result = fleet_ssh.run_ssh(ssh_argv, runner=runner, dry_run=dry_run)
        checks.append({"command_class": command_class, **result})
        if not result["ok"]:
            code = "repo_missing" if command_class == "probe_repo_exists" else "probe_failed"
            return {"ok": False, "checks": checks, "failure_code": code}
    return {"ok": True, "checks": checks}


def build_node_record(
    *,
    node_id: str,
    host: fleet_ssh.SshHostSpec,
    repo_root: str,
    state_dir: str,
    billing_accounts: list[str] | None,
    probe: dict[str, Any],
    added_at: str,
) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "status": "active",
        "ssh": {
            "alias": host.alias,
            "hostname": host.hostname,
            "user": host.user,
            "port": host.port,
            "identity_file": host.identity_file,
        },
        "repo_root": repo_root,
        "state_dir": state_dir,
        "billing_accounts": billing_accounts or [],
        "added_at": added_at,
        "last_probe_at": added_at,
        "probe": probe,
    }


def preview_node_add(fleet_doc: dict, node_id: str, node_entry: dict) -> dict[str, Any]:
    before = fleet_doc.get("nodes", {}).get(node_id)
    after_nodes = dict(fleet_doc.get("nodes") or {})
    after_nodes[node_id] = node_entry
    return {
        "node_id": node_id,
        "before": before,
        "after": node_entry,
        "fleet_nodes_count": len(after_nodes),
    }


def append_node_audit(fleet_dir: Path, entry: dict[str, Any]) -> None:
    path = fleet_dir / "audit" / "nodes.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, separators=(",", ":")) + "\n")


def save_node(
    fleet_dir: Path,
    node_id: str,
    node_entry: dict[str, Any],
    *,
    actor: str,
    iso_now: str,
) -> dict[str, Any]:
    import goalflight_fleet_store as fleet
    import goalflight_fleet_schemas as schemas

    fleet_path = fleet_dir / "fleet.json"
    with fleet.RegistryLock(fleet_dir):
        doc = fleet.read_json(fleet_path)
        schemas.validate_fleet(doc)
        nodes = dict(doc.get("nodes") or {})
        nodes[node_id] = node_entry
        doc["nodes"] = nodes
        schemas.validate_fleet(doc)
        fleet._atomic_write_json(fleet_path, doc)
        append_node_audit(
            fleet_dir,
            {
                "event": "node_add",
                "node_id": node_id,
                "actor": actor,
                "ts": iso_now,
                "repo_root": node_entry.get("repo_root"),
            },
        )
    return {"ok": True, "node_id": node_id, "path": str(fleet_path)}


def add_node_from_ssh(
    fleet_dir: Path,
    *,
    ssh_alias: str,
    repo_root: str,
    state_dir: str,
    node_id: str | None = None,
    billing_accounts: list[str] | None = None,
    ssh_config: Path | None = None,
    runner: Callable[[list[str]], tuple[int, str, str]] | None = None,
    dry_run: bool = False,
    actor: str = "cli",
    iso_now: str | None = None,
) -> dict[str, Any]:
    import goalflight_fleet_store as fleet

    iso_now = iso_now or fleet.iso()
    node_id = node_id or ssh_alias
    host = fleet_ssh.parse_ssh_config(ssh_alias, ssh_config)
    probe = run_node_probes(host, repo_root=repo_root, runner=runner, dry_run=dry_run)
    if not probe["ok"]:
        return {
            "ok": False,
            "stage": "probe",
            "failure_code": probe.get("failure_code"),
            "remediation": fleet_ssh.REMEDIATION_HINTS.get(
                str(probe.get("failure_code")), fleet_ssh.REMEDIATION_HINTS["probe_failed"]
            ),
            "checks": probe["checks"],
        }
    fleet_path = fleet_dir / "fleet.json"
    if not fleet_path.exists():
        fleet.bootstrap(fleet_dir)
    fleet_doc = fleet.read_json(fleet_path)
    node_entry = build_node_record(
        node_id=node_id,
        host=host,
        repo_root=repo_root,
        state_dir=state_dir,
        billing_accounts=billing_accounts,
        probe=probe,
        added_at=iso_now,
    )
    preview = preview_node_add(fleet_doc, node_id, node_entry)
    if dry_run:
        return {"ok": True, "dry_run": True, "preview": preview, "probe": probe}
    saved = save_node(fleet_dir, node_id, node_entry, actor=actor, iso_now=iso_now)
    return {
        "ok": True,
        "saved": saved,
        "preview": preview,
        "next_steps": [
            f"goalflight_fleet.py status --fleet-dir {fleet_dir}",
            "goalflight_fleet.py steering show --pending",
            f"example dispatch: ssh {ssh_alias} 'cd {repo_root} && python3 scripts/goalflight_acp_run.py --help'",
        ],
    }


def cmd_node_add(args) -> int:
    import goalflight_fleet_store as fleet

    repo_root = args.repo_root
    state_dir = args.state_dir
    if not repo_root:
        repo_root = input("Remote repo_root: ").strip()
    if not state_dir:
        state_dir = input("Remote state_dir [~/.goal-flight]: ").strip() or "~/.goal-flight"
    billing = [part.strip() for part in (args.billing_accounts or "").split(",") if part.strip()]
    result = add_node_from_ssh(
        args.fleet_dir,
        ssh_alias=args.from_ssh,
        repo_root=repo_root,
        state_dir=state_dir,
        node_id=args.node_id,
        billing_accounts=billing or None,
        ssh_config=args.ssh_config,
        dry_run=args.dry_run,
        actor=fleet.controller_id(),
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if not result.get("ok"):
            print(result.get("remediation") or "node add failed", file=__import__("sys").stderr)
            print(failure_remediation_table(), file=__import__("sys").stderr)
            return 1
        if result.get("dry_run"):
            print(json.dumps(result["preview"], indent=2))
        else:
            print(f"saved node {result['saved']['node_id']}")
            for step in result.get("next_steps", []):
                print(f"  - {step}")
    return 0 if result.get("ok") else 1
