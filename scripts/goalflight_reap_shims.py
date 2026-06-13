#!/usr/bin/env python3
"""Standalone CLI for orphaned claude-acp shim reporting and reaping."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from goalflight_acp_client import count_orphaned_acp_shims, reap_orphaned_acp_shims

SCHEMA = "goalflight.reap-shims.v1"


def format_dry_run_human(payload: dict[str, Any]) -> str:
    lines = [
        f"orphan_count={payload.get('orphan_count', 0)} "
        f"reapable_count={payload.get('reapable_count', 0)}",
    ]
    if payload.get("skipped"):
        lines.append(f"skipped: {payload['skipped']}")
    for orphan in payload.get("orphans") or []:
        pid = orphan.get("pid")
        age_s = orphan.get("age_s")
        comm = orphan.get("comm", "")
        owned = orphan.get("goalflight_owned")
        owned_tag = " goalflight_owned" if owned else ""
        lines.append(f"  pid={pid} age_s={age_s} comm={comm}{owned_tag}")
    lines.append("Re-run with --exec to reap goal-flight-owned orphans past TTL.")
    return "\n".join(lines)


def format_exec_human(payload: dict[str, Any]) -> str:
    if payload.get("skipped"):
        return f"skipped: {payload['skipped']}"
    if payload.get("error"):
        return f"error: {payload['error']}"
    reaped = payload.get("reaped") or []
    if not reaped:
        return "no orphaned shims reaped"
    lines = [f"reaped {len(reaped)} orphaned shim(s):"]
    for entry in reaped:
        lines.append(
            f"  pid={entry.get('pid')} age_s={entry.get('age_s')} "
            f"action={entry.get('action')}"
        )
    return "\n".join(lines)


def run_dry_run(*, as_json: bool) -> int:
    payload = count_orphaned_acp_shims()
    if as_json:
        out = {"schema": SCHEMA, "mode": "dry-run", **payload}
        print(json.dumps(out, indent=2))
    else:
        print(format_dry_run_human(payload))
    return 0


def run_exec(*, as_json: bool) -> int:
    payload = reap_orphaned_acp_shims()
    if as_json:
        out = {"schema": SCHEMA, "mode": "exec", **payload}
        print(json.dumps(out, indent=2))
    else:
        print(format_exec_human(payload))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report or reap orphaned goal-flight claude-acp shims.",
    )
    parser.add_argument(
        "--exec",
        action="store_true",
        help="reap goal-flight-owned orphans past TTL (default is dry-run only)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    if args.exec:
        return run_exec(as_json=args.json)
    return run_dry_run(as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())