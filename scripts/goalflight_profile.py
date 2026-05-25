#!/usr/bin/env python3
"""Worker profile loader for slotted dispatch (Track D Phase 0)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_PROFILES_DIR = Path.home() / ".goal-flight" / "profiles"
DEFAULT_READINESS_DIR = Path.home() / ".goal-flight" / "readiness"

GATEWAY_REQUIRED: dict[str, list[str]] = {
    "herm-worker": ["GOALFLIGHT_HERM_WORKER_URL"],
    "cla-worker": ["GOALFLIGHT_CLA_WORKER_URL"],
    "paperclip": ["GOALFLIGHT_PAPERCLIP_URL"],
}

GATEWAY_AGENTS = frozenset(GATEWAY_REQUIRED.keys())


def dispatch_env(
    agent: str,
    slot: str | None = None,
    *,
    profiles_dir: Path = DEFAULT_PROFILES_DIR,
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    """Merge gateway slot profile ENV into a subprocess environment."""
    env = dict(os.environ)
    if base:
        env.update(base)
    if agent not in GATEWAY_AGENTS:
        return env
    resolved_slot = slot or env.get("GOALFLIGHT_INSTALL_SLOT") or "default"
    path = profile_path(resolved_slot, profiles_dir)
    if path.exists():
        env.update(parse_env_file(path))
    env["GOALFLIGHT_INSTALL_SLOT"] = resolved_slot
    return env


def missing_gateway_profile_keys(
    agent: str,
    slot: str | None = None,
    *,
    profiles_dir: Path = DEFAULT_PROFILES_DIR,
) -> list[str]:
    if agent not in GATEWAY_AGENTS:
        return []
    resolved_slot = slot or os.environ.get("GOALFLIGHT_INSTALL_SLOT") or "default"
    path = profile_path(resolved_slot, profiles_dir)
    if not path.exists():
        return list(GATEWAY_REQUIRED.get(agent, []))
    values = parse_env_file(path)
    return [key for key in GATEWAY_REQUIRED.get(agent, []) if not values.get(key)]

LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)=(.*)$")


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = LINE_RE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        values[key] = os.path.expanduser(val)
    return values


def profile_path(slot: str, profiles_dir: Path) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9._-]", "", slot)
    if not safe:
        raise ValueError("invalid slot name")
    return profiles_dir / f"{safe}.env"


def readiness_path(agent_id: str, slot: str | None, readiness_dir: Path) -> Path:
    if slot:
        return readiness_dir / f"{agent_id}.{slot}.json"
    return readiness_dir / f"{agent_id}.json"


def cmd_load(args: argparse.Namespace) -> int:
    path = profile_path(args.slot, args.profiles_dir)
    if not path.exists():
        if args.allow_missing:
            print(json.dumps({"slot": args.slot, "path": str(path), "values": {}}))
            return 0
        print(f"profile not found: {path}", file=sys.stderr)
        return 1
    values = parse_env_file(path)
    print(json.dumps({"slot": args.slot, "path": str(path), "values": values}, indent=2))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    path = profile_path(args.slot, args.profiles_dir)
    if not path.exists():
        if args.allow_missing and not args.agent:
            print(json.dumps({"ok": True, "slot": args.slot, "missing_profile": True}))
            return 0
        print(f"profile not found: {path}", file=sys.stderr)
        return 1
    values = parse_env_file(path)
    missing: list[str] = []
    if args.agent:
        for key in GATEWAY_REQUIRED.get(args.agent, []):
            if not values.get(key):
                missing.append(key)
    if missing:
        print(json.dumps({"ok": False, "missing": missing}), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "slot": args.slot, "keys": sorted(values.keys())}))
    return 0


def cmd_print_export(args: argparse.Namespace) -> int:
    path = profile_path(args.slot, args.profiles_dir)
    if not path.exists():
        if args.allow_missing:
            return 0
        print(f"profile not found: {path}", file=sys.stderr)
        return 1
    for key, val in parse_env_file(path).items():
        escaped = val.replace("'", "'\\''")
        print(f"export {key}='{escaped}'")
    return 0


def cmd_readiness_path(args: argparse.Namespace) -> int:
    path = readiness_path(args.agent, args.slot, args.readiness_dir)
    print(json.dumps({"agent": args.agent, "slot": args.slot, "path": str(path)}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Goal Flight worker profiles")
    parser.add_argument("--profiles-dir", type=Path, default=DEFAULT_PROFILES_DIR)
    parser.add_argument("--readiness-dir", type=Path, default=DEFAULT_READINESS_DIR)
    sub = parser.add_subparsers(dest="cmd", required=True)

    load = sub.add_parser("load")
    load.add_argument("--slot", default="default")
    load.add_argument("--allow-missing", action="store_true")
    load.set_defaults(func=cmd_load)

    validate = sub.add_parser("validate")
    validate.add_argument("--slot", default="default")
    validate.add_argument("--agent", default="")
    validate.add_argument("--allow-missing", action="store_true")
    validate.set_defaults(func=cmd_validate)

    export = sub.add_parser("print-export")
    export.add_argument("--slot", default="default")
    export.add_argument("--allow-missing", action="store_true")
    export.set_defaults(func=cmd_print_export)

    rpath = sub.add_parser("readiness-path")
    rpath.add_argument("--agent", required=True)
    rpath.add_argument("--slot", default="")
    rpath.set_defaults(func=cmd_readiness_path)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
