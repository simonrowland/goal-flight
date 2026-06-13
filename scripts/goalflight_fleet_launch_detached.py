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
import hashlib
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

from goalflight_liveness import reset_status_lineage

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
MARKER_SCHEMA = "goalflight.fleet.launch_marker.v1"
RECOVERY_LOCK_SCHEMA = "goalflight.fleet.launch_recovery_lock.v1"
RECOVERY_LOCK_TTL_SECONDS = 60 * 60
NO_WORKER_PROOF_STATES = frozenset(
    {
        "prompt_write_failed",
        "spawn_failed",
        "exited_before_receipt",
    }
)
TERMINAL_NO_WORKER_STATES = frozenset(
    {
        "blocked",
        "blocked_auth",
        "blocked_capacity",
        "blocked_session_limit",
        "complete",
        "failed",
        "idle_timeout",
        "orphaned",
        "released",
        "superseded",
        "watcher_stopped",
        "worker_dead",
    }
)


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


def _ensure_local_bin_on_path(env: dict[str, str]) -> None:
    """Prepend ~/.local/bin to PATH in-place so a detached worker can find its shims.

    A non-login ``ssh host cmd`` invocation does not source ``~/.zprofile``, so the
    node-side PATH can omit ``~/.local/bin`` where ``setup_worker_path`` installs the
    agent shims -- notably ``claude``, which the claude-code-cli-acp ACP shim must
    exec. Without this, claude-acp on a non-sandboxed node clears pty allocation and
    then fails with ``-32603 "spawn claude pty"``. HOME is allow-listed by
    ``_sanitized_env``, so it reflects the node's home.
    """
    home = env.get("HOME")
    if not home:
        return
    local_bin = f"{home}/.local/bin"
    path = env.get("PATH", "")
    if local_bin in path.split(os.pathsep):
        return
    env["PATH"] = f"{local_bin}{os.pathsep}{path}" if path else local_bin


def _dispatch_dir(state_dir: Path, dispatch_id: str) -> Path:
    return state_dir / "dispatches" / dispatch_id


def _receipt_path(state_dir: Path, dispatch_id: str) -> Path:
    return _dispatch_dir(state_dir, dispatch_id) / "launch_receipt.json"


def _launch_marker_path(state_dir: Path, dispatch_id: str) -> Path:
    return _dispatch_dir(state_dir, dispatch_id) / "launch_marker.json"


def _recovery_lock_path(state_dir: Path, dispatch_id: str) -> Path:
    return _dispatch_dir(state_dir, dispatch_id) / "launch_recovery.lock"


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _create_json_exclusive(path: Path, payload: dict[str, Any]) -> bool:
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")
    return True


def _current_launcher_identity() -> dict[str, Any]:
    pid = os.getpid()
    identity = _process_identity(pid)
    if isinstance(identity, dict):
        return {**identity, "pid": pid}
    return {"pid": pid}


def _recovery_lock_payload(args: argparse.Namespace, proof_reason: str) -> dict[str, Any]:
    return {
        "schema": RECOVERY_LOCK_SCHEMA,
        "dispatch_id": args.dispatch_id,
        "node_id": args.node_id,
        "no_worker_proof": proof_reason,
        "launcher_pid": os.getpid(),
        "launcher_identity": _current_launcher_identity(),
        "created_at": _utc_now(),
    }


def _remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _prompt_sha256(prompt_text: str) -> str:
    return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()


def _marker_base(
    args: argparse.Namespace,
    state_dir: Path,
    status_json: Path,
    prompt_path: Path,
    log_path: Path,
    *,
    prompt_sha256: str,
    state: str,
) -> dict[str, Any]:
    return {
        "schema": MARKER_SCHEMA,
        "dispatch_id": args.dispatch_id,
        "node_id": args.node_id,
        "state": state,
        "remote_state_dir": str(state_dir),
        "remote_status_path": str(status_json),
        "prompt_path": str(prompt_path),
        "prompt_sha256": prompt_sha256,
        "launcher_log_path": str(log_path),
        "worktree_base_sha": getattr(args, "base_sha", ""),
        "updated_at": _utc_now(),
    }


def _update_launch_marker(marker_path: Path, updates: dict[str, Any]) -> None:
    payload = _load_json(marker_path) or {}
    payload.update(updates)
    payload.setdefault("schema", MARKER_SCHEMA)
    payload["updated_at"] = _utc_now()
    _atomic_write_json(marker_path, payload)


def _recorded_worker_live(pid_raw: Any, identity: Any) -> tuple[bool | None, str]:
    try:
        pid = int(pid_raw)
    except (TypeError, ValueError):
        return None, "no_pid"
    current = _process_identity(pid)
    if current is None:
        return False, "dead"
    if not isinstance(identity, dict):
        return None, "identity_missing"
    for key in ("lstart", "comm"):
        if identity.get(key) and current.get(key) and identity[key] != current[key]:
            return False, f"pid_reused_{key}"
    return True, "live"


def _parse_utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _recovery_lock_stale(path: Path, payload: dict[str, Any]) -> tuple[bool, str]:
    now = datetime.now(timezone.utc)
    created = _parse_utc_timestamp(payload.get("created_at"))
    source = "created_at"
    if created is None:
        source = "mtime"
        try:
            created = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        except OSError:
            return False, "timestamp_unavailable"
    age_s = max(0.0, (now - created).total_seconds())
    if age_s >= RECOVERY_LOCK_TTL_SECONDS:
        return True, f"stale_{source}"
    return False, f"fresh_{source}"


def _recovery_lock_owner_live(payload: dict[str, Any]) -> tuple[bool | None, str]:
    pid_raw = payload.get("launcher_pid") or payload.get("owner_pid") or payload.get("pid")
    identity = (
        payload.get("launcher_identity")
        or payload.get("owner_identity")
        or payload.get("identity")
    )
    try:
        int(pid_raw)
    except (TypeError, ValueError):
        return None, "no_pid"
    if not isinstance(identity, dict) or not identity.get("lstart"):
        return None, "identity_missing"
    return _recorded_worker_live(pid_raw, identity)


def _reclaim_recovery_lock_if_allowed(path: Path) -> tuple[bool, str]:
    try:
        observed = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return True, "lock_missing"
    except OSError as exc:
        return False, f"lock_unreadable:{exc.__class__.__name__}"

    try:
        parsed = json.loads(observed)
    except json.JSONDecodeError:
        parsed = {}
    payload = parsed if isinstance(parsed, dict) else {}

    live, live_reason = _recovery_lock_owner_live(payload)
    stale, stale_reason = _recovery_lock_stale(path, payload)
    if live is True:
        return False, "owner_live"
    if live is False:
        reclaim_reason = f"owner_{live_reason}"
    elif stale:
        reclaim_reason = f"owner_{live_reason}_{stale_reason}"
    else:
        return False, f"owner_{live_reason}_{stale_reason}"

    try:
        current = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return True, "lock_missing_after_check"
    except OSError as exc:
        return False, f"lock_recheck_failed:{exc.__class__.__name__}"
    if current != observed:
        return False, "lock_changed_during_reclaim"
    try:
        path.unlink()
    except FileNotFoundError:
        return True, "lock_missing_after_check"
    except OSError as exc:
        return False, f"lock_unlink_failed:{exc.__class__.__name__}"
    return True, reclaim_reason


def _status_no_worker_proof(status_json: Path) -> tuple[bool, str]:
    payload = _load_json(status_json)
    if not payload:
        return False, "status_missing"
    pid_raw = payload.get("worker_pid") or payload.get("pid")
    identity = payload.get("worker_identity") or payload.get("expected_worker_identity")
    live, reason = _recorded_worker_live(pid_raw, identity)
    if live is False:
        return True, f"status_worker_{reason}"
    if live is True:
        return False, "status_worker_live"
    state = str(payload.get("state") or "")
    if payload.get("worker_alive") is False and state in TERMINAL_NO_WORKER_STATES:
        return True, f"status_terminal_{state}"
    return False, reason


def _marker_no_worker_proof(marker: dict[str, Any] | None) -> tuple[bool, str]:
    if not marker:
        return False, "marker_missing"
    pid_raw = marker.get("remote_pid") or marker.get("worker_pid")
    identity = marker.get("remote_identity") or marker.get("worker_identity")
    live, reason = _recorded_worker_live(pid_raw, identity)
    if live is False:
        return True, f"marker_worker_{reason}"
    if live is True:
        return False, "marker_worker_live"
    state = str(marker.get("state") or "")
    if state in NO_WORKER_PROOF_STATES:
        return True, f"marker_state_{state}"
    return False, f"marker_{reason}"


def _no_worker_proof(status_json: Path, marker: dict[str, Any] | None) -> tuple[bool, str]:
    status_ok, status_reason = _status_no_worker_proof(status_json)
    if status_ok:
        return True, status_reason
    marker_ok, marker_reason = _marker_no_worker_proof(marker)
    if marker_ok:
        return True, marker_reason
    return False, f"{status_reason};{marker_reason}"


def _warn_refuse_duplicate(reason: str) -> int:
    print(
        "WARN-REFUSE duplicate dispatch-id exists on node; "
        "receipt recovery requires controller launch_unconfirmed evidence "
        f"and no-worker proof ({reason})",
        file=sys.stderr,
    )
    return 17


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
    marker_path = _launch_marker_path(state_dir, args.dispatch_id)
    recovery_lock_path = _recovery_lock_path(state_dir, args.dispatch_id)

    status_json = Path(args.status_json).expanduser()
    status_json.parent.mkdir(parents=True, exist_ok=True)
    log_path = dispatch_dir / "dispatcher.log"
    prompt_text = _decode_b64(args.prompt_b64)
    prompt_digest = _prompt_sha256(prompt_text)

    existing = _existing_receipt(args, status_json, state_dir)
    if existing:
        if not args.recover_unconfirmed:
            return _warn_refuse_duplicate("receipt_or_status_exists")
        existing["recovered"] = True
        print(json.dumps(existing, sort_keys=True))
        return 0

    marker_payload = _marker_base(
        args,
        state_dir,
        status_json,
        prompt_path,
        log_path,
        prompt_sha256=prompt_digest,
        state="launching",
    )
    marker_created = _create_json_exclusive(marker_path, marker_payload)
    recovery_lock_acquired = False
    recovery_lock_reclaim_reason = ""
    if not marker_created:
        marker = _load_json(marker_path)
        if not args.recover_unconfirmed:
            return _warn_refuse_duplicate("launch_marker_exists")
        if marker and marker.get("prompt_sha256") and marker.get("prompt_sha256") != prompt_digest:
            return _warn_refuse_duplicate("prompt_hash_mismatch")
        proof_ok, proof_reason = _no_worker_proof(status_json, marker)
        if not proof_ok:
            return _warn_refuse_duplicate(proof_reason)
        recovery_lock_payload = _recovery_lock_payload(args, proof_reason)
        recovery_lock_acquired = _create_json_exclusive(
            recovery_lock_path,
            recovery_lock_payload,
        )
        if not recovery_lock_acquired:
            reclaimed, reclaim_reason = _reclaim_recovery_lock_if_allowed(recovery_lock_path)
            if reclaimed:
                recovery_lock_reclaim_reason = reclaim_reason
                recovery_lock_payload["reclaimed_lock_reason"] = reclaim_reason
                recovery_lock_payload["reclaimed_at"] = _utc_now()
                recovery_lock_acquired = _create_json_exclusive(
                    recovery_lock_path,
                    recovery_lock_payload,
                )
        if not recovery_lock_acquired:
            return _warn_refuse_duplicate("recovery_already_in_progress")
        previous_state = marker.get("state") if marker else None
        marker_payload["state"] = "recovery_launching"
        marker_payload["recovered_from_marker_state"] = previous_state
        marker_payload["no_worker_proof"] = proof_reason
        if recovery_lock_reclaim_reason:
            marker_payload["reclaimed_recovery_lock"] = recovery_lock_reclaim_reason
        _atomic_write_json(marker_path, marker_payload)

    if recovery_lock_acquired:
        reset_status_lineage(status_json)

    try:
        prompt_path.write_text(prompt_text, encoding="utf-8")
    except OSError as exc:
        _update_launch_marker(
            marker_path,
            {
                "state": "prompt_write_failed",
                "error": str(exc),
            },
        )
        if recovery_lock_acquired:
            _remove_file(recovery_lock_path)
        print(
            json.dumps(
                {
                    "ok": False,
                    "dispatch_id": args.dispatch_id,
                    "node_id": args.node_id,
                    "error": f"prompt write failed: {exc}",
                    "prompt_path": str(prompt_path),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

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
    _ensure_local_bin_on_path(env)

    popen_cmd = cmd
    if os.name != "nt":
        nohup = shutil.which("nohup")
        if nohup:
            popen_cmd = [nohup, *cmd]

    try:
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
    except OSError as exc:
        _update_launch_marker(
            marker_path,
            {
                "state": "spawn_failed",
                "error": str(exc),
            },
        )
        if recovery_lock_acquired:
            _remove_file(recovery_lock_path)
        print(
            json.dumps(
                {
                    "ok": False,
                    "dispatch_id": args.dispatch_id,
                    "node_id": args.node_id,
                    "error": f"detached dispatcher spawn failed: {exc}",
                    "launcher_log_path": str(log_path),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    identity = _process_identity_after_spawn(proc.pid)
    _update_launch_marker(
        marker_path,
        {
            "state": "spawned",
            "remote_pid": proc.pid,
            "remote_lstart": (identity or {}).get("lstart"),
            "remote_identity": identity,
            "spawned_at": _utc_now(),
        },
    )
    if proc.poll() is not None:
        _update_launch_marker(
            marker_path,
            {
                "state": "exited_before_receipt",
                "exit_code": proc.returncode,
            },
        )
        if recovery_lock_acquired:
            _remove_file(recovery_lock_path)
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
    _update_launch_marker(
        marker_path,
        {
            "state": "receipted",
            "receipt_path": str(receipt_file),
            "remote_pid": proc.pid,
            "remote_lstart": (identity or {}).get("lstart"),
            "remote_identity": identity,
            "receipted_at": receipt["started_at"],
        },
    )
    if recovery_lock_acquired:
        _remove_file(recovery_lock_path)
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
