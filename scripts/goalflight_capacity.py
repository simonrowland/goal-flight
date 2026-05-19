#!/usr/bin/env python3
"""Machine-global capacity coordinator for goal-flight dispatches."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import uuid

SCHEMA = "goalflight.capacity.v1"


def _default_state_dir() -> Path:
    return Path("/tmp") / f"goal-flight-{os.getuid()}"


DEFAULT_STATE_DIR = Path(os.environ.get("GOALFLIGHT_STATE_DIR", _default_state_dir()))
DEFAULT_RESERVE_MB = 2048
DEFAULT_WORST_WORKER_MB = 1200
DEFAULT_HARD_CAP = 20
AGENT_RSS_MB = {
    "grok": 111,
    "codex": 386,
    "codex-acp": 386,
    "claude": 614,
    "claude-code-cli-acp": 614,
    "cursor": 1203,
    "cursor-agent": 1203,
}
# Per-agent concurrency caps, machine-global across goal-flight sessions.
# Sized to support multi-session parallel work. The actual safety net against
# rate-limit pressure is an adaptive busy-signal walkback (deferred follow-up
# — see docs-private/claude-rate-limit-recipe-2026-05-19.md §Adaptive pacing).
# Until that lands, these static caps + the SKILL.md routing table (prefer
# non-Claude workers for code-writing dispatches) carry the policy load.
DEFAULT_AGENT_CAPS = {
    "cursor": 3,
    "cursor-agent": 3,
    "claude": 5,
    "claude-code-cli-acp": 5,
    "codex": 10,
    "codex-acp": 10,
    "grok": 10,
}
TERMINAL_LEASE_STATES = {
    "released",
    "expired",
    "complete",
    "failed",
    "blocked",
    "blocked_capacity",
    "blocked_session_limit",
    "blocked_auth",
    "inconclusive_timeout",
    "inconclusive_no_final",
    "superseded",
}


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(ts: dt.datetime | None = None) -> str:
    return (ts or utc_now()).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def state_dir() -> Path:
    path = Path(os.environ.get("GOALFLIGHT_STATE_DIR", str(DEFAULT_STATE_DIR))).expanduser()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def state_path() -> Path:
    return state_dir() / "capacity.json"


def lock_path() -> Path:
    return state_dir() / "capacity.lock"


class StateLock:
    def __enter__(self):
        lock_path().parent.mkdir(parents=True, exist_ok=True)
        self._fh = lock_path().open("w")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()


def load_state() -> dict:
    path = state_path()
    if not path.exists():
        return {"schema": SCHEMA, "machine_id": machine_id(), "leases": {}, "cooldowns": {}}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        data = {"schema": SCHEMA, "machine_id": machine_id(), "leases": {}, "cooldowns": {}}
    data.setdefault("schema", SCHEMA)
    data.setdefault("machine_id", machine_id())
    data.setdefault("leases", {})
    data.setdefault("cooldowns", {})
    return data


def save_state(data: dict) -> None:
    data["updated_at"] = iso()
    tmp = state_path().with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(state_path())


def machine_id() -> str:
    return f"{socket.gethostname()}:{platform.machine()}"


def run_text(cmd: list[str], timeout: float = 2.0) -> str | None:
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=timeout).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def detect_ram_mb() -> int:
    if sys.platform == "darwin":
        out = run_text(["sysctl", "-n", "hw.memsize"])
        if out and out.isdigit():
            return int(out) // 1024 // 1024
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemTotal:"):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    return int(parts[1]) // 1024
    return 0


def detect_tools() -> dict:
    grok = shutil.which("grok") or str(Path.home() / ".grok/bin/grok")
    tools = {
        "codex": bool(shutil.which("codex")),
        "codex-acp": bool(shutil.which("codex-acp")),
        "claude": bool(shutil.which("claude")),
        "claude-code-cli-acp": bool(shutil.which("claude-code-cli-acp")),
        "cursor": bool(shutil.which("cursor")),
        "cursor-agent": bool(shutil.which("cursor-agent")),
        "grok": Path(grok).exists() if grok else False,
    }
    return tools


def operating_cap_for_ram(ram_mb: int, raw_ceiling: int) -> int:
    override = os.environ.get("GOALFLIGHT_CAPACITY_MAX_TOTAL")
    if override:
        try:
            return max(1, min(raw_ceiling, int(override)))
        except ValueError:
            pass
    if ram_mb <= 0:
        tier = 2
    elif ram_mb <= 8 * 1024:
        tier = 1
    elif ram_mb <= 16 * 1024:
        tier = 3
    elif ram_mb <= 32 * 1024:
        tier = 4
    elif ram_mb <= 64 * 1024:
        tier = 6
    else:
        tier = 16  # >64GB: 16 workers (was 8). Headroom for multi-session
                   # parallel work now that per-agent caps allow codex/grok=10.
    return max(1, min(raw_ceiling, tier))


def profile(args: argparse.Namespace | None = None) -> dict:
    ram_mb = getattr(args, "ram_mb", None) or detect_ram_mb()
    reserve_mb = getattr(args, "reserve_mb", None) or DEFAULT_RESERVE_MB
    worst_worker_mb = getattr(args, "worst_worker_mb", None) or DEFAULT_WORST_WORKER_MB
    hard_cap = getattr(args, "hard_cap", None) or DEFAULT_HARD_CAP
    headroom_mb = max(0, ram_mb - reserve_mb)
    raw_ceiling = max(1, min(hard_cap, headroom_mb // worst_worker_mb if worst_worker_mb else 1))
    max_total = getattr(args, "max_total", None)
    if max_total:
        operating_cap = max(1, min(raw_ceiling, max_total))
    else:
        operating_cap = operating_cap_for_ram(ram_mb, raw_ceiling)
    return {
        "schema": "goalflight.capacity.profile.v1",
        "machine_id": machine_id(),
        "ram_mb": ram_mb,
        "cpu_count": os.cpu_count() or 0,
        "controller_reserve_mb": reserve_mb,
        "worst_case_worker_mb": worst_worker_mb,
        "raw_ram_ceiling": raw_ceiling,
        "operating_cap": operating_cap,
        "hard_cap": hard_cap,
        "agent_caps": DEFAULT_AGENT_CAPS,
        "agent_rss_mb": AGENT_RSS_MB,
        "tools": detect_tools(),
    }


def prune_state(data: dict) -> None:
    now = utc_now()
    leases = data.get("leases", {})
    for lease_id in list(leases):
        lease = leases[lease_id]
        expires_at = parse_iso(lease.get("expires_at"))
        if expires_at and expires_at < now:
            lease["state"] = "expired"
            lease["ended_at"] = lease.get("ended_at") or iso()
        terminal_at = parse_iso(lease.get("released_at") or lease.get("ended_at"))
        if lease.get("state") in TERMINAL_LEASE_STATES and terminal_at:
            if now - terminal_at < dt.timedelta(hours=24):
                continue
            leases.pop(lease_id, None)
        elif lease.get("state") in TERMINAL_LEASE_STATES:
            leases.pop(lease_id, None)
    cooldowns = data.get("cooldowns", {})
    for agent in list(cooldowns):
        until = parse_iso(cooldowns[agent].get("until"))
        if until and until < now:
            cooldowns.pop(agent, None)


def normalize_agent(agent: str) -> str:
    return agent.strip().lower()


def active_leases(data: dict) -> list[dict]:
    return [lease for lease in data.get("leases", {}).values() if lease.get("state") == "active"]


def cooldown_for(data: dict, agent: str) -> dict | None:
    cooldowns = data.get("cooldowns", {})
    return cooldowns.get(agent) or cooldowns.get(agent.split("-")[0])


def cmd_profile(args: argparse.Namespace) -> int:
    payload = profile(args)
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"capacity: ram={payload['ram_mb']}MB raw={payload['raw_ram_ceiling']} operating={payload['operating_cap']}")
        print(f"tools: {', '.join(k for k, v in payload['tools'].items() if v) or 'none'}")
    return 0


def cmd_acquire(args: argparse.Namespace) -> int:
    agent = normalize_agent(args.agent)
    prof = profile(args)
    rss_mb = args.mem_mb or AGENT_RSS_MB.get(agent, DEFAULT_WORST_WORKER_MB)
    with StateLock():
        data = load_state()
        prune_state(data)
        cooldown = cooldown_for(data, agent)
        if cooldown:
            payload = {
                "decision": "wait",
                "reason": f"cooldown:{cooldown.get('reason', 'unspecified')}",
                "agent": agent,
                "retry_after_s": max(0, int((parse_iso(cooldown.get("until")) - utc_now()).total_seconds())) if parse_iso(cooldown.get("until")) else None,
                "cooldown": cooldown,
            }
            save_state(data)
            print(json.dumps(payload, sort_keys=True))
            return 2

        leases = active_leases(data)
        max_total = args.max_total or prof["operating_cap"]
        agent_cap = args.agent_cap or DEFAULT_AGENT_CAPS.get(agent, 2)
        agent_count = sum(1 for lease in leases if normalize_agent(lease.get("agent", "")) == agent)
        total_rss = sum(int(lease.get("mem_mb") or 0) for lease in leases)
        if len(leases) >= max_total:
            payload = {"decision": "wait", "reason": "machine_worker_cap", "active": len(leases), "max_total": max_total}
            save_state(data)
            print(json.dumps(payload, sort_keys=True))
            return 2
        if agent_count >= agent_cap:
            payload = {"decision": "wait", "reason": "agent_worker_cap", "agent": agent, "active": agent_count, "agent_cap": agent_cap}
            save_state(data)
            print(json.dumps(payload, sort_keys=True))
            return 2
        if prof["ram_mb"] and total_rss + rss_mb > max(0, prof["ram_mb"] - prof["controller_reserve_mb"]):
            payload = {"decision": "wait", "reason": "rss_budget", "active_rss_mb": total_rss, "request_mem_mb": rss_mb}
            save_state(data)
            print(json.dumps(payload, sort_keys=True))
            return 2

        lease_id = args.lease_id or str(uuid.uuid4())
        ttl = dt.timedelta(seconds=args.ttl_s)
        lease = {
            "lease_id": lease_id,
            "dispatch_id": args.dispatch_id,
            "prompt_id": args.prompt_id,
            "agent": agent,
            "project_root": args.project_root,
            "controller_pid": args.controller_pid or os.getpid(),
            "worker_pid": args.worker_pid,
            "mem_mb": rss_mb,
            "state": "active",
            "started_at": iso(),
            "expires_at": iso(utc_now() + ttl),
        }
        data.setdefault("leases", {})[lease_id] = lease
        save_state(data)
    print(json.dumps({"decision": "allow", "lease": lease, "profile": prof}, sort_keys=True))
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    with StateLock():
        data = load_state()
        lease = data.get("leases", {}).get(args.lease_id)
        if not lease:
            print(json.dumps({"ok": False, "reason": "missing_lease", "lease_id": args.lease_id}, sort_keys=True))
            return 1
        lease["state"] = args.state
        lease["released_at"] = iso()
        if args.reason:
            lease["reason"] = args.reason
        if args.keep:
            save_state(data)
        else:
            data.get("leases", {}).pop(args.lease_id, None)
            save_state(data)
    print(json.dumps({"ok": True, "lease_id": args.lease_id, "state": args.state}, sort_keys=True))
    return 0


def cmd_cooldown(args: argparse.Namespace) -> int:
    agent = normalize_agent(args.agent)
    with StateLock():
        data = load_state()
        prune_state(data)
        if args.action == "clear":
            data.get("cooldowns", {}).pop(agent, None)
            save_state(data)
            print(json.dumps({"ok": True, "agent": agent, "action": "clear"}, sort_keys=True))
            return 0
        until = utc_now() + dt.timedelta(seconds=args.seconds)
        data.setdefault("cooldowns", {})[agent] = {
            "agent": agent,
            "reason": args.reason,
            "until": iso(until),
            "recorded_at": iso(),
        }
        save_state(data)
    print(json.dumps({"ok": True, "agent": agent, "until": iso(until), "reason": args.reason}, sort_keys=True))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    with StateLock():
        data = load_state()
        prune_state(data)
        save_state(data)
    payload = {"schema": SCHEMA, "profile": profile(args), "state": data, "active": active_leases(data)}
    if args.json:
        print(json.dumps(payload, sort_keys=True))
        return 0
    prof = payload["profile"]
    print(f"capacity: active={len(payload['active'])}/{prof['operating_cap']} raw={prof['raw_ram_ceiling']} ram={prof['ram_mb']}MB")
    for lease in payload["active"]:
        print(f"- {lease['lease_id']} agent={lease['agent']} dispatch={lease.get('dispatch_id')} mem={lease.get('mem_mb')}MB")
    if data.get("cooldowns"):
        print("cooldowns:")
        for cooldown in data["cooldowns"].values():
            print(f"- {cooldown['agent']}: {cooldown.get('reason')} until {cooldown.get('until')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="goal-flight machine capacity coordinator")
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--ram-mb", type=int)
    parent.add_argument("--reserve-mb", type=int, default=DEFAULT_RESERVE_MB)
    parent.add_argument("--worst-worker-mb", type=int, default=DEFAULT_WORST_WORKER_MB)
    parent.add_argument("--hard-cap", type=int, default=DEFAULT_HARD_CAP)
    parent.add_argument("--max-total", type=int)

    sub = parser.add_subparsers(dest="cmd", required=True)
    prof = sub.add_parser("profile", parents=[parent])
    prof.add_argument("--json", action="store_true")
    prof.set_defaults(func=cmd_profile)

    acq = sub.add_parser("acquire", parents=[parent])
    acq.add_argument("--agent", required=True)
    acq.add_argument("--dispatch-id")
    acq.add_argument("--prompt-id")
    acq.add_argument("--project-root")
    acq.add_argument("--controller-pid", type=int)
    acq.add_argument("--worker-pid", type=int)
    acq.add_argument("--lease-id")
    acq.add_argument("--mem-mb", type=int)
    acq.add_argument("--agent-cap", type=int)
    acq.add_argument("--ttl-s", type=int, default=8 * 60 * 60)
    acq.set_defaults(func=cmd_acquire)

    rel = sub.add_parser("release")
    rel.add_argument("--lease-id", required=True)
    rel.add_argument("--state", default="released")
    rel.add_argument("--reason")
    rel.add_argument("--keep", action="store_true")
    rel.set_defaults(func=cmd_release)

    cool = sub.add_parser("cooldown")
    cool.add_argument("action", choices=["set", "clear"])
    cool.add_argument("--agent", required=True)
    cool.add_argument("--seconds", type=int, default=3600)
    cool.add_argument("--reason", default="rate_limit")
    cool.set_defaults(func=cmd_cooldown)

    stat = sub.add_parser("status", parents=[parent])
    stat.add_argument("--json", action="store_true")
    stat.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
