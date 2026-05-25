#!/usr/bin/env python3
"""Machine-local goal-flight dispatch ledger.

Records process identity next to prompt/session metadata so controllers can
recover after sleep, compaction, or parallel session overlap without reading
raw logs into the model context.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import uuid

SCHEMA = "goalflight.dispatch.v1"


def _default_state_dir() -> Path:
    return Path("/tmp") / f"goal-flight-{os.getuid()}"


DEFAULT_STATE_DIR = Path(os.environ.get("GOALFLIGHT_STATE_DIR", _default_state_dir()))
WORKER_PATTERNS = (
    "codex",
    "codex-acp",
    "grok",
    "cursor-agent",
    "claude-code-cli-acp",
    "opencode",
    "opencode-acp",
    "opencode-bash-tail",
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def state_dir() -> Path:
    return Path(os.environ.get("GOALFLIGHT_STATE_DIR", str(DEFAULT_STATE_DIR))).expanduser()


def runs_dir() -> Path:
    path = state_dir() / "runs.d"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def lock_path() -> Path:
    path = state_dir()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path / "ledger.lock"


class StateLock:
    def __enter__(self):
        self._fh = lock_path().open("w")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()


def sha256_file(path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists() or not p.is_file():
        return None
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _ps_field(pid: int, field: str) -> str | None:
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", f"{field}="],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return out or None


def process_identity(pid: int | None) -> dict | None:
    if not pid:
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    ident = {
        "pid": pid,
        "ppid": _ps_field(pid, "ppid"),
        "pgid": _ps_field(pid, "pgid"),
        "lstart": _ps_field(pid, "lstart"),
        "comm": _ps_field(pid, "comm"),
        "args": _ps_field(pid, "args"),
    }
    return ident


def identity_matches(record: dict) -> tuple[bool, str]:
    pid = record.get("worker_pid") or record.get("controller_pid")
    if not pid:
        return False, "no_pid"
    current = process_identity(int(pid))
    if current is None:
        return False, "dead"
    prior = record.get("worker_identity") or record.get("controller_identity") or {}
    for key in ("lstart", "comm"):
        if prior.get(key) and current.get(key) and prior[key] != current[key]:
            return False, f"pid_reused_{key}"
    return True, "live"


def classify(record: dict) -> str:
    state = record.get("state", "running")
    terminal_states = {
        "complete",
        "failed",
        "blocked",
        "released",
        "superseded",
        "blocked_capacity",
        "blocked_session_limit",
        "blocked_auth",
        "inconclusive_timeout",
        "inconclusive_no_final",
    }
    if state in terminal_states:
        return state
    ok, reason = identity_matches(record)
    if ok:
        return "expected_live"
    if reason == "no_pid":
        return "unknown_no_pid"
    return f"stale_{reason}"


def record_path(dispatch_id: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in dispatch_id)
    if safe != dispatch_id:
        safe = f"{safe}-{hashlib.sha256(dispatch_id.encode()).hexdigest()[:8]}"
    return runs_dir() / f"{safe}.json"


def write_record(record: dict) -> Path:
    record["updated_at"] = utc_now()
    path = record_path(record["dispatch_id"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)
    return path


def read_records() -> list[dict]:
    records: list[dict] = []
    for p in sorted(runs_dir().glob("*.json")):
        try:
            records.append(json.loads(p.read_text()))
        except (OSError, json.JSONDecodeError):
            records.append({"schema": SCHEMA, "dispatch_id": p.stem, "state": "unreadable", "path": str(p)})
    return records


def scan_surplus(records: list[dict], limit: int = 20) -> list[dict]:
    known = {int(r["worker_pid"]) for r in records if r.get("worker_pid")}
    known.update(int(r["controller_pid"]) for r in records if r.get("controller_pid"))
    try:
        out = subprocess.check_output(
            ["ps", "ax", "-o", "pid=", "-o", "comm=", "-o", "args="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    surplus: list[dict] = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid in known:
            continue
        comm = parts[1]
        args = parts[2] if len(parts) > 2 else ""
        haystack = f"{comm} {args}"
        if any(pattern in haystack for pattern in WORKER_PATTERNS):
            surplus.append({"pid": pid, "comm": comm, "args": args[:240]})
        if len(surplus) >= limit:
            break
    return surplus


def cmd_record(args: argparse.Namespace) -> int:
    dispatch_id = args.dispatch_id or str(uuid.uuid4())
    worker_identity = process_identity(args.worker_pid)
    controller_pid = args.controller_pid or os.getpid()
    os_sandbox = None
    if getattr(args, "os_sandbox_json", None):
        try:
            os_sandbox = json.loads(args.os_sandbox_json)
        except json.JSONDecodeError:
            os_sandbox = {"raw": args.os_sandbox_json}
    record = {
        "schema": SCHEMA,
        "dispatch_id": dispatch_id,
        "prompt_id": args.prompt_id,
        "prompt_path": args.prompt_path,
        "prompt_sha256": sha256_file(args.prompt_path),
        "agent": args.agent,
        "transport": args.transport,
        "project_root": args.project_root,
        "controller_pid": controller_pid,
        "controller_identity": process_identity(controller_pid),
        "worker_pid": args.worker_pid,
        "worker_identity": worker_identity,
        "worker_pgid": worker_identity.get("pgid") if worker_identity else None,
        "acp_session_id": args.acp_session_id,
        "logical_session_id": args.logical_session_id,
        "lease_id": args.lease_id,
        "stdout_path": args.stdout_path,
        "stderr_path": args.stderr_path,
        "status_path": args.status_path,
        "os_sandbox": os_sandbox,
        "state": args.state,
        "started_at": utc_now(),
        "hostname": socket.gethostname(),
    }
    with StateLock():
        path = write_record(record)
    payload = {"ok": True, "dispatch_id": dispatch_id, "path": str(path), "state": record["state"]}
    print(json.dumps(payload, indent=None if args.json else 2, sort_keys=True))
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    path = record_path(args.dispatch_id)
    if not path.exists():
        print(json.dumps({"ok": False, "error": "missing_dispatch", "dispatch_id": args.dispatch_id}))
        return 1
    with StateLock():
        record = json.loads(path.read_text())
        record["state"] = args.state
        record["ended_at"] = utc_now()
        if args.reason:
            record["reason"] = args.reason
        write_record(record)
    print(json.dumps({"ok": True, "dispatch_id": args.dispatch_id, "state": args.state}, sort_keys=True))
    return 0


def status_payload() -> dict:
    records = read_records()
    rows = []
    for r in records:
        row = {
            "dispatch_id": r.get("dispatch_id"),
            "prompt_id": r.get("prompt_id"),
            "agent": r.get("agent"),
            "transport": r.get("transport"),
            "state": r.get("state"),
            "classification": classify(r),
            "worker_pid": r.get("worker_pid"),
            "project_root": r.get("project_root"),
            "status_path": r.get("status_path"),
            "os_sandbox": r.get("os_sandbox"),
            "started_at": r.get("started_at"),
            "updated_at": r.get("updated_at"),
        }
        rows.append(row)
    return {
        "schema": SCHEMA,
        "state_dir": str(state_dir()),
        "records": rows,
        "surplus_processes": scan_surplus(records),
    }


def cmd_status(args: argparse.Namespace) -> int:
    payload = status_payload()
    if args.json:
        print(json.dumps(payload, sort_keys=True))
        return 0
    print(f"dispatch ledger: {payload['state_dir']}")
    for row in payload["records"][: args.limit]:
        print(
            f"- {row['classification']}: {row.get('dispatch_id')} "
            f"agent={row.get('agent')} pid={row.get('worker_pid')} state={row.get('state')}"
        )
    if payload["surplus_processes"]:
        print("surplus worker-like processes:")
        for proc in payload["surplus_processes"][: args.limit]:
            print(f"- pid={proc['pid']} comm={proc['comm']} args={proc['args']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="goal-flight dispatch ledger")
    sub = parser.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record")
    rec.add_argument("--dispatch-id")
    rec.add_argument("--prompt-id")
    rec.add_argument("--prompt-path")
    rec.add_argument("--agent", required=True)
    rec.add_argument("--transport", default="unknown")
    rec.add_argument("--project-root")
    rec.add_argument("--controller-pid", type=int)
    rec.add_argument("--worker-pid", type=int)
    rec.add_argument("--acp-session-id")
    rec.add_argument("--logical-session-id")
    rec.add_argument("--lease-id")
    rec.add_argument("--stdout-path")
    rec.add_argument("--stderr-path")
    rec.add_argument("--status-path")
    rec.add_argument("--os-sandbox-json")
    rec.add_argument("--state", default="running")
    rec.add_argument("--json", action="store_true")
    rec.set_defaults(func=cmd_record)

    fin = sub.add_parser("finish")
    fin.add_argument("--dispatch-id", required=True)
    fin.add_argument("--state", default="complete")
    fin.add_argument("--reason")
    fin.set_defaults(func=cmd_finish)

    stat = sub.add_parser("status")
    stat.add_argument("--json", action="store_true")
    stat.add_argument("--limit", type=int, default=20)
    stat.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
