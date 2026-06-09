#!/usr/bin/env python3
"""Billing account link/unlink and auth probes for fleet dispatch (Track A goal 8)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

import goalflight_fleet_ssh as fleet_ssh

AUTH_PROBE_SCHEMA = "goalflight.fleet.auth-probe.v1"
ProbeRunner = Callable[[list[str]], tuple[int, str, str]]
INCONCLUSIVE_STATUS = "inconclusive"

PROVIDER_BY_ACCOUNT: dict[str, str] = {
    "openai": "openai",
    "anthropic": "anthropic-session",
    "anthropic-session": "anthropic-session",
    "grok": "grok",
    "cursor": "cursor",
}

LOCAL_PROBE_ARGV: dict[str, list[str]] = {
    "openai": ["codex", "login", "status"],
    "anthropic-session": ["claude", "--version"],
    "grok": [
        "sh",
        "-lc",
        'test -s "${HOME}/.grok/auth.json" && echo logged_in',
    ],
    "cursor": ["cursor-agent", "status"],
}

TOOLING_FAILURE_EXIT_CODES = {126, 127}
TOOLING_FAILURE_RE = re.compile(
    r"(command not found|not found:|no such file or directory|can't open file|"
    r"bare-python-not-found|probe_failed|transient|temporar|timeout|connection refused|"
    r"permission denied \(publickey\)|ssh:)",
    re.I,
)
AUTH_DENIED_RE = re.compile(
    r"(not logged[_ -]?in|not authenticated|unauthenticated|authentication required|"
    r"login required|no api key|invalid api key|expired credential|not authorized|unauthorized)",
    re.I,
)

KEYCHAIN_LOCKED_RE = re.compile(r"keychain.*locked", re.I)
CURSOR_AUTH_NEGATIVE_RE = re.compile(
    r"not logged in|not authenticated|login required|please log in",
    re.I,
)
CURSOR_AUTH_POSITIVE_RE = re.compile(
    r"logged in as|login successful|✓ logged in",
    re.I,
)


class DispatchAuthError(Exception):
    def __init__(self, message: str, *, auth_probe: str = "red") -> None:
        self.auth_probe = auth_probe
        super().__init__(message)


def default_runner(argv: list[str]) -> tuple[int, str, str]:
    import os

    try:
        env = os.environ.copy()
        home = Path.home()
        extra = os.pathsep.join(
            [
                str(home / ".local/bin"),
                str(home / ".grok/bin"),
                "/opt/homebrew/bin",
                "/usr/local/bin",
            ]
        )
        if extra not in env.get("PATH", ""):
            env["PATH"] = f"{extra}:{env.get('PATH', '')}"
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
            env=env,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)


def provider_for_account(account_key: str, billing_doc: dict | None) -> str:
    if billing_doc:
        for account in billing_doc.get("accounts") or []:
            if isinstance(account, dict) and account.get("account_key") == account_key:
                provider = account.get("provider")
                if isinstance(provider, str) and provider:
                    return provider
    prefix = account_key.split("/", 1)[0]
    return PROVIDER_BY_ACCOUNT.get(prefix, prefix)


def probe_argv(provider: str) -> list[str]:
    if provider not in LOCAL_PROBE_ARGV:
        raise ValueError(f"no auth probe configured for provider: {provider}")
    return list(LOCAL_PROBE_ARGV[provider])


def is_inconclusive_probe(payload: dict[str, Any] | None) -> bool:
    return str((payload or {}).get("status") or "") == INCONCLUSIVE_STATUS


def _non_auth_probe_failure(exit_code: int, stdout: str, stderr: str) -> bool:
    combined = f"{stdout}\n{stderr}"
    return exit_code in TOOLING_FAILURE_EXIT_CODES or bool(TOOLING_FAILURE_RE.search(combined))


def _failed_probe_status(exit_code: int, stdout: str, stderr: str) -> str:
    combined = f"{stdout}\n{stderr}"
    if AUTH_DENIED_RE.search(combined):
        return "red"
    if _non_auth_probe_failure(exit_code, stdout, stderr):
        return INCONCLUSIVE_STATUS
    return INCONCLUSIVE_STATUS


def interpret_auth_probe(provider: str, exit_code: int, stdout: str, stderr: str) -> str:
    combined = f"{stdout}\n{stderr}"
    if provider == "openai":
        if exit_code == 0 and re.search(r"logged[_ -]?in", combined, re.I):
            return "green"
        if exit_code == 0:
            return "yellow"
        return _failed_probe_status(exit_code, stdout, stderr)
    if provider == "anthropic-session":
        if exit_code == 0 and stdout.strip():
            return "green"
        return _failed_probe_status(exit_code, stdout, stderr)
    if provider == "grok":
        if exit_code == 0 and re.search(r"logged_in", combined, re.I):
            return "green"
        if exit_code == 1:
            return "red"
        if _non_auth_probe_failure(exit_code, stdout, stderr):
            return INCONCLUSIVE_STATUS
        return "red"
    if provider == "cursor":
        if KEYCHAIN_LOCKED_RE.search(combined):
            return "yellow"
        if CURSOR_AUTH_NEGATIVE_RE.search(combined):
            return "red"
        if exit_code == 0 and CURSOR_AUTH_POSITIVE_RE.search(combined):
            return "green"
        return _failed_probe_status(exit_code, stdout, stderr)
    return "green" if exit_code == 0 else _failed_probe_status(exit_code, stdout, stderr)


def run_local_auth_probe(
    account_key: str,
    billing_doc: dict | None,
    *,
    runner: ProbeRunner | None = None,
    iso_now: str | None = None,
) -> dict[str, Any]:
    import goalflight_fleet as fleet

    runner = runner or default_runner
    iso_now = iso_now or fleet.iso()
    provider = provider_for_account(account_key, billing_doc)
    argv = probe_argv(provider)
    exit_code, stdout, stderr = runner(argv)
    status = interpret_auth_probe(provider, exit_code, stdout, stderr)
    return {
        "schema": AUTH_PROBE_SCHEMA,
        "account_key": account_key,
        "provider": provider,
        "status": status,
        "exit_code": exit_code,
        "stdout_tail": stdout.strip()[-400:],
        "stderr_tail": stderr.strip()[-400:],
        "probed_at": iso_now,
        "mode": "local",
    }


def probe_artifact_path(fleet_dir: Path, node_id: str, account_key: str) -> Path:
    safe_node = node_id.replace("/", "__")
    safe_account = account_key.replace("/", "__")
    return fleet_dir / "probes" / f"{safe_node}__{safe_account}.json"


def write_probe_artifact(fleet_dir: Path, node_id: str, payload: dict[str, Any]) -> Path:
    fleet_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = probe_artifact_path(fleet_dir, node_id, str(payload["account_key"]))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    enriched = dict(payload)
    enriched["node_id"] = node_id
    path.write_text(json.dumps(enriched, indent=2) + "\n")
    return path


def read_probe_artifact(fleet_dir: Path, node_id: str, account_key: str) -> dict[str, Any] | None:
    path = probe_artifact_path(fleet_dir, node_id, account_key)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _remote_probe_payload_from_stdout(stdout: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("schema") != AUTH_PROBE_SCHEMA:
        return None
    return payload


def run_node_auth_probe(
    fleet_dir: Path,
    node_id: str,
    account_key: str,
    *,
    billing_doc: dict | None = None,
    runner: ProbeRunner | None = None,
    ssh_runner: ProbeRunner | None = None,
    iso_now: str | None = None,
    write_artifact: bool = True,
) -> dict[str, Any]:
    import goalflight_fleet as fleet

    iso_now = iso_now or fleet.iso()
    fleet_path = fleet_dir / "fleet.json"
    fleet_doc = fleet.read_json(fleet_path)
    node = (fleet_doc.get("nodes") or {}).get(node_id)
    if not isinstance(node, dict):
        raise ValueError(f"unknown node: {node_id}")
    if billing_doc is None:
        billing_path = fleet_dir / "billing-accounts.json"
        billing_doc = fleet.read_json(billing_path) if billing_path.exists() else None

    ssh_info = node.get("ssh") or {}
    alias = str(ssh_info.get("alias") or node_id)
    host = fleet_ssh.SshHostSpec(
        alias=alias,
        hostname=str(ssh_info.get("hostname") or alias),
        user=ssh_info.get("user"),
        port=ssh_info.get("port"),
        identity_file=ssh_info.get("identity_file"),
    )
    repo_root = str(node.get("repo_root") or "")
    if alias in {"localhost", "local"} or ssh_info.get("hostname") in {"localhost", "127.0.0.1"}:
        payload = run_local_auth_probe(account_key, billing_doc, runner=runner, iso_now=iso_now)
        payload["node_id"] = node_id
    else:
        remote = fleet_ssh.build_remote_command(
            "auth_probe",
            repo_root=repo_root,
            state_dir=str(node.get("state_dir") or "~/.goal-flight"),
            account_key=account_key,
        )
        ssh_argv = fleet_ssh.build_ssh_command(host, remote, command_class="auth_probe")
        result = fleet_ssh.run_ssh(ssh_argv, runner=ssh_runner, dry_run=False)
        remote_payload = _remote_probe_payload_from_stdout(str(result.get("stdout") or ""))
        if remote_payload is not None:
            payload = remote_payload
        elif not result["ok"]:
            payload = {
                "schema": AUTH_PROBE_SCHEMA,
                "node_id": node_id,
                "account_key": account_key,
                "provider": provider_for_account(account_key, billing_doc),
                "status": INCONCLUSIVE_STATUS,
                "exit_code": result.get("exit_code", 1),
                "stderr_tail": str(result.get("stderr", ""))[-400:],
                "probed_at": iso_now,
                "mode": "remote",
                "failure_code": result.get("failure_code", "probe_failed"),
                "detail": "remote auth probe failed before returning an auth verdict",
            }
        else:
            payload = {
                "schema": AUTH_PROBE_SCHEMA,
                "node_id": node_id,
                "account_key": account_key,
                "provider": provider_for_account(account_key, billing_doc),
                "status": INCONCLUSIVE_STATUS,
                "probed_at": iso_now,
                "mode": "remote",
                "detail": "invalid probe JSON",
            }
        payload.setdefault("node_id", node_id)
        payload.setdefault("account_key", account_key)
        payload.setdefault("mode", "remote")
        payload.setdefault("probed_at", iso_now)

    if write_artifact:
        write_probe_artifact(fleet_dir, node_id, payload)
    return payload


def link_account_to_node(
    fleet_dir: Path,
    account_key: str,
    node_id: str,
    *,
    run_probe: bool = True,
    runner: ProbeRunner | None = None,
    ssh_runner: ProbeRunner | None = None,
    iso_now: str | None = None,
) -> dict[str, Any]:
    import goalflight_fleet as fleet
    import goalflight_fleet_schemas as schemas

    iso_now = iso_now or fleet.iso()
    fleet_path = fleet_dir / "fleet.json"
    billing_path = fleet_dir / "billing-accounts.json"
    with fleet.RegistryLock(fleet_dir):
        fleet_doc = fleet.read_json(fleet_path)
        billing_doc = fleet.read_json(billing_path)
        schemas.validate_fleet(fleet_doc)
        schemas.validate_billing_accounts(billing_doc)
        known = {
            str(item.get("account_key"))
            for item in (billing_doc.get("accounts") or [])
            if isinstance(item, dict) and item.get("account_key")
        }
        if account_key not in known:
            raise ValueError(f"unknown billing account_key: {account_key}")
        nodes = dict(fleet_doc.get("nodes") or {})
        node = nodes.get(node_id)
        if not isinstance(node, dict):
            raise ValueError(f"unknown node: {node_id}")
        linked = list(node.get("billing_accounts") or [])
        if account_key not in linked:
            linked.append(account_key)
        node["billing_accounts"] = linked
        nodes[node_id] = node
        fleet_doc["nodes"] = nodes
        schemas.validate_fleet(fleet_doc)
        fleet._atomic_write_json(fleet_path, fleet_doc)

    probe_payload = None
    if run_probe:
        probe_payload = run_node_auth_probe(
            fleet_dir,
            node_id,
            account_key,
            billing_doc=billing_doc,
            runner=runner,
            ssh_runner=ssh_runner,
            iso_now=iso_now,
        )
    return {"ok": True, "account_key": account_key, "node_id": node_id, "auth_probe": probe_payload}


def unlink_account_from_node(fleet_dir: Path, account_key: str, node_id: str) -> dict[str, Any]:
    import goalflight_fleet as fleet
    import goalflight_fleet_schemas as schemas

    fleet_path = fleet_dir / "fleet.json"
    with fleet.RegistryLock(fleet_dir):
        fleet_doc = fleet.read_json(fleet_path)
        schemas.validate_fleet(fleet_doc)
        nodes = dict(fleet_doc.get("nodes") or {})
        node = nodes.get(node_id)
        if not isinstance(node, dict):
            raise ValueError(f"unknown node: {node_id}")
        linked = [key for key in (node.get("billing_accounts") or []) if key != account_key]
        node["billing_accounts"] = linked
        nodes[node_id] = node
        fleet_doc["nodes"] = nodes
        schemas.validate_fleet(fleet_doc)
        fleet._atomic_write_json(fleet_path, fleet_doc)
    artifact = probe_artifact_path(fleet_dir, node_id, account_key)
    if artifact.exists():
        artifact.unlink()
    return {"ok": True, "account_key": account_key, "node_id": node_id}


def fleet_auth_doctor(
    fleet_dir: Path,
    *,
    refresh: bool = False,
    runner: ProbeRunner | None = None,
    ssh_runner: ProbeRunner | None = None,
) -> dict[str, Any]:
    import goalflight_fleet as fleet

    fleet_path = fleet_dir / "fleet.json"
    if not fleet_path.exists():
        return {"available": False, "reason": "fleet store missing", "nodes": []}
    fleet_doc = fleet.read_json(fleet_path)
    nodes_out: list[dict[str, Any]] = []
    for node_id, node in sorted((fleet_doc.get("nodes") or {}).items()):
        if not isinstance(node, dict):
            continue
        accounts_out: list[dict[str, Any]] = []
        for account_key in node.get("billing_accounts") or []:
            payload = read_probe_artifact(fleet_dir, node_id, str(account_key))
            if refresh or payload is None or is_inconclusive_probe(payload):
                payload = run_node_auth_probe(
                    fleet_dir,
                    node_id,
                    str(account_key),
                    runner=runner,
                    ssh_runner=ssh_runner,
                    write_artifact=True,
                )
            accounts_out.append(
                {
                    "account_key": account_key,
                    "auth_probe": payload.get("status", "red"),
                    "provider": payload.get("provider"),
                    "probed_at": payload.get("probed_at"),
                    "probe_path": str(probe_artifact_path(fleet_dir, node_id, str(account_key))),
                }
            )
        nodes_out.append({"node_id": node_id, "accounts": accounts_out})
    return {
        "available": True,
        "fleet_dir": str(fleet_dir),
        "nodes": nodes_out,
    }


def assert_dispatch_auth(fleet_dir: Path, node_id: str, account_key: str) -> None:
    payload = read_probe_artifact(fleet_dir, node_id, account_key)
    if payload is None:
        raise DispatchAuthError(
            f"missing auth probe for node={node_id} account={account_key}",
            auth_probe="red",
        )
    status = str(payload.get("status") or "red")
    if status != "green":
        raise DispatchAuthError(
            f"auth probe {status} for node={node_id} account={account_key}",
            auth_probe=status,
        )


def cmd_account_link(args) -> int:
    import goalflight_fleet as fleet

    try:
        result = link_account_to_node(
            args.fleet_dir,
            args.account_key,
            args.node,
            run_probe=not args.skip_probe,
        )
    except ValueError as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        probe = result.get("auth_probe") or {}
        print(f"linked {args.account_key} -> {args.node} auth_probe={probe.get('status', 'skipped')}")
    return 0


def cmd_account_unlink(args) -> int:
    try:
        result = unlink_account_from_node(args.fleet_dir, args.account_key, args.node)
    except ValueError as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"unlinked {args.account_key} from {args.node}")
    return 0


def cmd_probe(args) -> int:
    import goalflight_fleet as fleet

    billing_path = args.fleet_dir / "billing-accounts.json"
    billing_doc = fleet.read_json(billing_path) if billing_path.exists() else None
    if args.node:
        payload = run_node_auth_probe(
            args.fleet_dir,
            args.node,
            args.account_key,
            billing_doc=billing_doc,
        )
    else:
        payload = run_local_auth_probe(args.account_key, billing_doc)
    print(json.dumps(payload, indent=2))
    return 0 if payload.get("status") == "green" else 1


def main(argv: list[str] | None = None) -> int:
    import argparse
    import goalflight_fleet as fleet

    parser = argparse.ArgumentParser(description="Fleet billing auth probes")
    parser.add_argument("--fleet-dir", type=Path, default=fleet.default_fleet_dir())
    sub = parser.add_subparsers(dest="cmd", required=True)

    probe = sub.add_parser("probe")
    probe.add_argument("--account-key", required=True)
    probe.add_argument("--node")
    probe.set_defaults(func=cmd_probe)

    link = sub.add_parser("link")
    link.add_argument("--account-key", required=True)
    link.add_argument("--node", required=True)
    link.add_argument("--skip-probe", action="store_true")
    link.add_argument("--json", action="store_true")
    link.set_defaults(func=cmd_account_link)

    unlink = sub.add_parser("unlink")
    unlink.add_argument("--account-key", required=True)
    unlink.add_argument("--node", required=True)
    unlink.add_argument("--json", action="store_true")
    unlink.set_defaults(func=cmd_account_unlink)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
