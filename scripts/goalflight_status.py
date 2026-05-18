#!/usr/bin/env python3
"""Compact status aggregator for goal-flight runtime state."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import goalflight_capacity
import goalflight_ledger


def status_payload() -> dict:
    with goalflight_capacity.StateLock():
        capacity_state = goalflight_capacity.load_state()
        goalflight_capacity.prune_state(capacity_state)
        goalflight_capacity.save_state(capacity_state)
    return {
        "schema": "goalflight.status.aggregate.v1",
        "capacity": goalflight_capacity.profile(argparse.Namespace()),
        "capacity_state": capacity_state,
        "dispatch": goalflight_ledger.status_payload(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="goal-flight compact status")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    payload = status_payload()
    if args.json:
        print(json.dumps(payload, sort_keys=True))
        return 0

    cap = payload["capacity"]
    leases = [l for l in payload["capacity_state"].get("leases", {}).values() if l.get("state") == "active"]
    print(f"capacity: active={len(leases)}/{cap.get('operating_cap')} raw={cap.get('raw_ram_ceiling')} ram={cap.get('ram_mb')}MB")
    cooldowns = payload["capacity_state"].get("cooldowns", {})
    if cooldowns:
        print("cooldowns:")
        for item in list(cooldowns.values())[: args.limit]:
            print(f"- {item.get('agent')}: {item.get('reason')} until {item.get('until')}")
    records = payload["dispatch"].get("records", [])
    if records:
        print("dispatches:")
        for row in records[: args.limit]:
            print(f"- {row.get('classification')}: {row.get('dispatch_id')} agent={row.get('agent')} pid={row.get('worker_pid')}")
    surplus = payload["dispatch"].get("surplus_processes", [])
    if surplus:
        print("surplus worker-like processes:")
        for proc in surplus[: args.limit]:
            print(f"- pid={proc.get('pid')} comm={proc.get('comm')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
