#!/usr/bin/env python3
"""Load and validate Goal Flight action registry entries (Track B Phase 0)."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
import sys

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_ACTIONS_DIR = REPO_ROOT / "config" / "actions"
DEFAULT_SCHEMA = REPO_ROOT / "schemas" / "goalflight.action.v1.json"
DEFAULT_COMMANDS_ENV = REPO_ROOT / "config" / "commands.env"
COMMANDS_ENV_REPO_ROOT = "$GOALFLIGHT_ROOT"
ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")
sys.path.insert(0, str(SCRIPT_DIR))

import goalflight_compat  # noqa: E402


def _windows_shell_arg(value: str) -> str:
    return subprocess.list2cmdline([value])


def _substitute(value: str, env: dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        return env.get(key, match.group(0))

    return ENV_PATTERN.sub(repl, value)


def _load_yaml_docs(path: Path) -> list[dict]:
    if yaml is None:
        raise RuntimeError("PyYAML required: pip install pyyaml")
    docs = list(yaml.safe_load_all(path.read_text()))
    entries = [d for d in docs if isinstance(d, dict)]
    if not entries:
        raise ValueError(f"{path}: expected at least one mapping")
    return entries


def _minimal_validate(entry: dict, schema_path: Path) -> list[str]:
    errors: list[str] = []
    for key in ("id", "domain", "resource", "verb", "command"):
        if not entry.get(key):
            errors.append(f"missing required field: {key}")
    action_id = entry.get("id", "")
    if action_id and not re.match(r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9-]*)+$", action_id):
        errors.append(f"invalid id format: {action_id}")
    if schema_path.exists():
        try:
            import jsonschema
        except ImportError:
            return errors
        schema = json.loads(schema_path.read_text())
        validator = jsonschema.Draft202012Validator(schema)
        errors.extend(f"{e.message}" for e in validator.iter_errors(entry))
    return errors


def load_actions(actions_dir: Path) -> dict[str, dict]:
    actions: dict[str, dict] = {}
    if not actions_dir.is_dir():
        return actions
    for path in sorted(actions_dir.glob("*.yaml")):
        for entry in _load_yaml_docs(path):
            action_id = entry.get("id")
            if not action_id:
                raise ValueError(f"{path}: missing id")
            if action_id in actions:
                raise ValueError(f"duplicate action id {action_id} in {path}")
            actions[action_id] = entry
    return actions


def find_action(actions: dict[str, dict], domain: str, resource: str, verb: str) -> str | None:
    for action_id, entry in actions.items():
        if (
            entry.get("domain") == domain
            and entry.get("resource") == resource
            and entry.get("verb") == verb
        ):
            return action_id
    return None


def _resolve_env(entry: dict, extra_env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("GOALFLIGHT_REPO_ROOT", str(REPO_ROOT))
    env.setdefault("GOALFLIGHT_PYTHON", "python3")
    if extra_env:
        env.update(extra_env)
    merged: dict[str, str] = {**env, **(entry.get("env") or {})}
    if goalflight_compat.is_windows() and merged.get("GOALFLIGHT_PYTHON") == "python3":
        merged["GOALFLIGHT_PYTHON"] = os.environ.get("GOALFLIGHT_PYTHON") or goalflight_compat.python_executable()
    for _ in range(4):
        changed = False
        for key, val in list(merged.items()):
            resolved = _substitute(str(val), merged)
            if resolved != val:
                merged[key] = resolved
                changed = True
        if not changed:
            break
    if goalflight_compat.is_windows():
        for key in ("GOALFLIGHT_PYTHON", "GOALFLIGHT_REPO_ROOT", "GOALFLIGHT_REPO"):
            if key in merged:
                merged[key] = _windows_shell_arg(str(merged[key]))
    return merged


def resolve_command(entry: dict, extra_env: dict[str, str] | None = None) -> str:
    merged = _resolve_env(entry, extra_env)
    cmd = _substitute(str(entry["command"]).strip(), merged)
    for _ in range(3):
        if "${" not in cmd:
            break
        cmd = _substitute(cmd, merged)
    return cmd


def cmd_list(args: argparse.Namespace) -> int:
    actions = load_actions(args.actions_dir)
    for action_id in sorted(actions):
        entry = actions[action_id]
        summary = entry.get("summary") or ""
        print(f"{action_id}\t{summary}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    actions = load_actions(args.actions_dir)
    errors: list[str] = []
    for action_id, entry in actions.items():
        for err in _minimal_validate(entry, args.schema):
            errors.append(f"{action_id}: {err}")
    if errors:
        for line in errors:
            print(line, file=sys.stderr)
        return 1
    print(f"actions_valid={len(actions)}")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    actions = load_actions(args.actions_dir)
    entry = actions.get(args.action_id)
    if not entry:
        print(f"unknown action: {args.action_id}", file=sys.stderr)
        return 1
    print(resolve_command(entry))
    return 0


def cmd_route(args: argparse.Namespace) -> int:
    actions = load_actions(args.actions_dir)
    action_id = find_action(actions, args.domain, args.resource, args.verb)
    if not action_id:
        print(f"no action for {args.domain} {args.resource} {args.verb}", file=sys.stderr)
        return 1
    cmd = resolve_command(actions[action_id])
    if not args.exec:
        print(cmd)
        return 0
    proc = subprocess.run(cmd, shell=True)
    return proc.returncode


def _env_export_name(action_id: str) -> str:
    return "GF_ACTION_" + action_id.replace(".", "_").replace("-", "_")


def cmd_commands_env(args: argparse.Namespace) -> int:
    actions = load_actions(args.actions_dir)
    lines = [
        "# Generated by goalflight_actions.py commands-env — idempotent, no secrets",
        f"# action_count={len(actions)}",
        ': "${GOALFLIGHT_ROOT:=$HOME/.goal-flight}"',
    ]
    for action_id in sorted(actions):
        cmd = resolve_command(actions[action_id], {"GOALFLIGHT_REPO_ROOT": COMMANDS_ENV_REPO_ROOT})
        export_name = _env_export_name(action_id)
        escaped = cmd.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`")
        lines.append(f'export {export_name}="{escaped}"')
    content = "\n".join(lines) + "\n"
    out = args.out
    if out.exists() and out.read_text() == content:
        print(f"commands_env_unchanged={out}")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content)
    print(f"commands_env_written={out}")
    return 0


SETUP_DESTINATION_ACTIONS: dict[str, str] = {
    "codex-cli-worker": "core.dispatch.execute",
    "codex-desktop-controller": "core.doctor.read",
    "cursor-cli-worker": "core.dispatch.execute",
    "cursor-desktop-controller": "core.doctor.read",
    "cursor-project-controller": "core.doctor.read",
    "cursor-agents-standard-controller": "core.doctor.read",
    "cursor-claude-link-controller": "core.doctor.read",
}


def _load_setup_destinations(repo_root: Path) -> list[tuple[str, str, str]]:
    adapters_dir = repo_root / "adapters"
    rows: list[tuple[str, str, str]] = []
    if not adapters_dir.is_dir():
        return rows
    for path in sorted(adapters_dir.glob("*.json")):
        if path.name.endswith(".schema.json"):
            continue
        try:
            manifest = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        agent_id = manifest.get("agent_id")
        if not agent_id:
            continue
        setup = manifest.get("setup") or {}
        for role in ("controller_destinations", "worker_destinations"):
            for destination in setup.get(role, []) or []:
                dest_id = destination.get("id")
                if not dest_id:
                    continue
                action_id = SETUP_DESTINATION_ACTIONS.get(dest_id, "(unmapped)")
                rows.append((agent_id, dest_id, action_id))
    return rows


def cmd_setup_map(args: argparse.Namespace) -> int:
    rows = _load_setup_destinations(args.repo_root)
    if args.json:
        payload = [
            {"agent_id": agent, "destination_id": dest, "action_id": action_id}
            for agent, dest, action_id in rows
        ]
        print(json.dumps(payload, indent=2))
        return 0
    print("agent_id\tdestination_id\taction_id")
    for agent, dest, action_id in rows:
        print(f"{agent}\t{dest}\t{action_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Goal Flight action registry")
    parser.add_argument("--actions-dir", type=Path, default=DEFAULT_ACTIONS_DIR)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list").set_defaults(func=cmd_list)
    sub.add_parser("validate").set_defaults(func=cmd_validate)
    render = sub.add_parser("render")
    render.add_argument("action_id")
    render.set_defaults(func=cmd_render)

    route = sub.add_parser("route")
    route.add_argument("domain")
    route.add_argument("resource")
    route.add_argument("verb")
    route.add_argument("--exec", action="store_true", help="Run the resolved command (default is dry-run print)")
    route.set_defaults(func=cmd_route)

    env = sub.add_parser("commands-env")
    env.add_argument("--out", type=Path, default=DEFAULT_COMMANDS_ENV)
    env.set_defaults(func=cmd_commands_env)

    setup_map = sub.add_parser("setup-map")
    setup_map.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    setup_map.add_argument("--json", action="store_true")
    setup_map.set_defaults(func=cmd_setup_map)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
