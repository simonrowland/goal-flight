#!/usr/bin/env python3
"""Remote fleet helper: detach the node-local Goal Flight dispatcher.

This script is intentionally small and argv-shaped so fleet SSH can allowlist it
without allowing arbitrary shell fragments. The remote node runs this helper; it
starts that node's own ``goalflight_dispatch.py`` in a new session with stdout
and stderr redirected to files, then returns a launch receipt immediately.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

ENV_ALLOW_EXACT = frozenset(
    {
        "CODEX_HOME",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "PATH",
        "SHELL",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "USER",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    }
)
ENV_ALLOW_PREFIXES = (
    "ANTHROPIC_",
    "CODEX_",
    "CURSOR_",
    "GOALFLIGHT_",
    "GROK_",
    "LC_",
    "OPENAI_",
    "XDG_",
)
ENV_DENY_EXACT = frozenset({"SSH_AUTH_SOCK", "GPG_AGENT_INFO"})
ENV_DENY_SUBSTRINGS = ("AUTH_SOCK", "AGENT_SOCK", "SSH_AGENT")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _decode_b64(value: str) -> str:
    return base64.b64decode(value.encode("ascii")).decode("utf-8")


def _process_identity(pid: int) -> dict[str, Any] | None:
    ledger_identity: dict[str, Any] | None = None
    try:
        import goalflight_ledger

        identity = goalflight_ledger.process_identity(pid)
        if identity and identity.get("lstart"):
            return identity
        if isinstance(identity, dict):
            ledger_identity = identity
    except Exception:
        pass

    try:
        lstart = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            check=False,
        )
        comm = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ledger_identity
    if lstart.returncode != 0 or not lstart.stdout.strip():
        return ledger_identity
    fallback = {
        "pid": pid,
        "lstart": lstart.stdout.strip(),
        "comm": comm.stdout.strip() if comm.returncode == 0 else "",
    }
    if ledger_identity:
        fallback = {**ledger_identity, **{k: v for k, v in fallback.items() if v}}
    return fallback


def _process_identity_after_spawn(pid: int) -> dict[str, Any] | None:
    identity = None
    for _ in range(20):
        current = _process_identity(pid)
        if current:
            identity = current
        if current and current.get("lstart"):
            break
        time.sleep(0.05)
    return identity


def _sanitized_env(source: dict[str, str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in source.items():
        if key in ENV_DENY_EXACT or any(part in key for part in ENV_DENY_SUBSTRINGS):
            continue
        if key in ENV_ALLOW_EXACT or any(key.startswith(prefix) for prefix in ENV_ALLOW_PREFIXES):
            env[key] = value
    return env


def _dispatch_dir(state_dir: Path, dispatch_id: str) -> Path:
    return state_dir / "dispatches" / dispatch_id


def _receipt_path(state_dir: Path, dispatch_id: str) -> Path:
    return _dispatch_dir(state_dir, dispatch_id) / "launch_receipt.json"


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _receipt_from_status(args: argparse.Namespace, status_json: Path, state_dir: Path) -> dict[str, Any] | None:
    payload = _load_json(status_json)
    if not payload:
        return None
    pid_raw = payload.get("worker_pid") or payload.get("pid")
    try:
        pid = int(pid_raw)
    except (TypeError, ValueError):
        return None
    identity = payload.get("worker_identity") or payload.get("expected_worker_identity")
    if not isinstance(identity, dict):
        identity = _process_identity(pid)
    return {
        "schema": "goalflight.fleet.launch_receipt.v1",
        "dispatch_id": args.dispatch_id,
        "node_id": args.node_id,
        "remote_pid": pid,
        "remote_lstart": (identity or {}).get("lstart"),
        "remote_identity": identity,
        "remote_status_path": str(status_json),
        "remote_state_dir": str(state_dir),
        "prompt_path": str(_dispatch_dir(state_dir, args.dispatch_id) / "prompt.md"),
        "launcher_log_path": str(_dispatch_dir(state_dir, args.dispatch_id) / "dispatcher.log"),
        "started_at": str(payload.get("updated_at") or _utc_now()),
        "worktree_base_sha": getattr(args, "base_sha", ""),
        "reused": True,
        "reuse_source": "status_json",
    }


def _existing_receipt(args: argparse.Namespace, status_json: Path, state_dir: Path) -> dict[str, Any] | None:
    receipt_file = _receipt_path(state_dir, args.dispatch_id)
    receipt = _load_json(receipt_file)
    if receipt and receipt.get("dispatch_id") == args.dispatch_id:
        reused = dict(receipt)
        reused.setdefault("worktree_base_sha", getattr(args, "base_sha", ""))
        reused["reused"] = True
        reused["reuse_source"] = "launch_receipt"
        return reused
    return _receipt_from_status(args, status_json, state_dir)


def _launch(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).expanduser()
    state_dir = Path(args.state_dir).expanduser()
    dispatch_dir = _dispatch_dir(state_dir, args.dispatch_id)
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = dispatch_dir / "prompt.md"

    status_json = Path(args.status_json).expanduser()
    status_json.parent.mkdir(parents=True, exist_ok=True)
    log_path = dispatch_dir / "dispatcher.log"

    existing = _existing_receipt(args, status_json, state_dir)
    if existing:
        if not args.recover_unconfirmed:
            print(
                "WARN-REFUSE duplicate dispatch-id exists on node; "
                "receipt recovery requires controller launch_unconfirmed evidence",
                file=sys.stderr,
            )
            return 17
        existing["recovered"] = True
        print(json.dumps(existing, sort_keys=True))
        return 0

    prompt_path.write_text(_decode_b64(args.prompt_b64))

    dispatch_py = repo_root / "scripts" / "goalflight_dispatch.py"
    cmd = [
        sys.executable,
        str(dispatch_py),
        "--agent",
        args.agent,
        "--shape",
        "acp",
        "--prompt-file",
        str(prompt_path),
        "--cwd",
        args.cwd,
        "--dispatch-id",
        args.dispatch_id,
        "--status-json",
        str(status_json),
    ]
    if args.read_only:
        cmd.append("--read-only")

    env = _sanitized_env(os.environ)
    env["GOALFLIGHT_STATE_DIR"] = str(state_dir)
    env["GOALFLIGHT_FLEET_NODE_ID"] = args.node_id

    popen_cmd = cmd
    if os.name != "nt":
        nohup = shutil.which("nohup")
        if nohup:
            popen_cmd = [nohup, *cmd]

    with open(os.devnull, "rb") as stdin_f, open(log_path, "ab") as log_f:
        proc = subprocess.Popen(
            popen_cmd,
            stdin=stdin_f,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(repo_root),
            start_new_session=(os.name != "nt"),
            close_fds=True,
        )

    identity = _process_identity_after_spawn(proc.pid)
    if proc.poll() is not None:
        print(
            json.dumps(
                {
                    "ok": False,
                    "dispatch_id": args.dispatch_id,
                    "node_id": args.node_id,
                    "error": "detached dispatcher exited before receipt",
                    "exit_code": proc.returncode,
                    "launcher_log_path": str(log_path),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    receipt = {
        "schema": "goalflight.fleet.launch_receipt.v1",
        "dispatch_id": args.dispatch_id,
        "node_id": args.node_id,
        "remote_pid": proc.pid,
        "remote_lstart": (identity or {}).get("lstart"),
        "remote_identity": identity,
        "remote_status_path": str(status_json),
        "remote_state_dir": str(state_dir),
        "prompt_path": str(prompt_path),
        "launcher_log_path": str(log_path),
        "started_at": _utc_now(),
        "worktree_base_sha": getattr(args, "base_sha", ""),
    }
    receipt_file = _receipt_path(state_dir, args.dispatch_id)
    receipt_file.write_text(json.dumps(receipt, sort_keys=True) + "\n")
    print(json.dumps(receipt, sort_keys=True))
    return 0


def _pid_identity(args: argparse.Namespace) -> int:
    expected_lstart = _decode_b64(args.expected_lstart_b64) if args.expected_lstart_b64 else ""
    identity = _process_identity(args.pid)
    alive = identity is not None
    if expected_lstart:
        alive = bool(identity and identity.get("lstart") == expected_lstart)
    payload = {
        "schema": "goalflight.fleet.pid_identity.v1",
        "pid": args.pid,
        "alive": alive,
        "identity": identity,
        "expected_lstart": expected_lstart or None,
        "checked_at": _utc_now(),
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Goal Flight fleet detached launch helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    launch = sub.add_parser("launch")
    launch.add_argument("--repo-root", required=True)
    launch.add_argument("--node-id", required=True)
    launch.add_argument("--dispatch-id", required=True)
    launch.add_argument("--agent", required=True)
    launch.add_argument("--prompt-b64", required=True)
    launch.add_argument("--cwd", required=True)
    launch.add_argument("--state-dir", required=True)
    launch.add_argument("--status-json", required=True)
    launch.add_argument("--read-only", action="store_true")
    launch.add_argument("--recover-unconfirmed", action="store_true")
    launch.add_argument("--base-sha", required=True)
    launch.add_argument("--json", action="store_true")
    launch.set_defaults(func=_launch)

    ident = sub.add_parser("pid-identity")
    ident.add_argument("--pid", type=int, required=True)
    ident.add_argument("--expected-lstart-b64")
    ident.add_argument("--json", action="store_true")
    ident.set_defaults(func=_pid_identity)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
