#!/usr/bin/env python3
"""Stdlib-only static validation for Goal Flight adapter manifests."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from goalflight_adapter_gate import (  # noqa: E402
    find_forbidden_args,
    manifest_forbidden_patterns,
)


EXPECTED_ADAPTERS = {
    "claude-code",
    "codex",
    "cursor",
    "grok",
    "hermes",
    "openclaw",
    "gemini",
    "qwen",
    "opencode",
    "kilocode",
    "kiro",
    "copilot",
    "openhands",
    "pi",
    "amp",
    "antigravity",
}

SURVEY_WORKER_STUBS = {
    "gemini",
    "qwen",
    "opencode",
    "kilocode",
    "kiro",
    "copilot",
    "openhands",
    "pi",
    "amp",
    "antigravity",
}

HOST_TOOL_RE = re.compile(
    r"\b(functions\.[A-Za-z_][A-Za-z0-9_]*|mcp__[A-Za-z0-9_]+__\w+|"
    r"ctx_(?:batch_execute|execute_file|execute|search|fetch_and_index|index)|"
    r"AskUserQuestion|TodoWrite|Browser plugin|Chrome plugin)\b|"
    r"\bSkill\s*\(|\bAgent\s+tool\b"
)

NO_LEAK_SKIP_PREFIXES = (
    ".git/",
    ".pytest_cache/",
    ".claude/",
    ".claude-plugin/",
    ".codex/",
    "adapters/",
    "docs-private/",
    "scripts/",
    "test/",
    "tests/",
)

NO_LEAK_COMPAT_ALLOWLIST = {
    "CHANGELOG.md",
    "protocols/dispatch-routing.md",
    "prompts/ask-anticipatory.md",
    "prompts/gstack-claude-review.md",
}

NO_LEAK_COMPAT_PREFIXES = (
    "prompts/",
    "protocols/legacy/",
)

PACKAGE_PROBE_COMMANDS = {"npm", "npx", "pip", "pip3", "brew", "cargo", "gem"}
UPDATE_PROBE_WORDS = {"update", "upgrade", "install", "add", "search", "view", "info"}


class AdapterValidationError(ValueError):
    """Validation error carrying one or more path-qualified errors."""

    def __init__(self, errors: Iterable[str]):
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))


def _json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def _resolve_ref(schema: dict[str, Any], root_schema: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if not isinstance(ref, str):
        return schema
    if not ref.startswith("#/"):
        raise AdapterValidationError([f"schema: unsupported external ref {ref}"])
    node: Any = root_schema
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        node = node[part]
    if not isinstance(node, dict):
        raise AdapterValidationError([f"schema: ref {ref} does not resolve to object"])
    return node


def _validate_schema_node(
    value: Any,
    schema: dict[str, Any],
    root_schema: dict[str, Any],
    path: str,
    errors: list[str],
) -> None:
    schema = _resolve_ref(schema, root_schema)

    expected_type = schema.get("type")
    if expected_type is not None:
        expected_types = expected_type if isinstance(expected_type, list) else [expected_type]
        if not any(_type_matches(value, str(item)) for item in expected_types):
            errors.append(f"{path}: expected type {expected_types}, got {_json_type_name(value)}")
            return

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {value!r}")

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: expected one of {schema['enum']!r}, got {value!r}")

    if isinstance(value, str) and "minLength" in schema:
        if len(value) < int(schema["minLength"]):
            errors.append(f"{path}: string shorter than minLength {schema['minLength']}")

    if isinstance(value, int) and not isinstance(value, bool) and "minimum" in schema:
        if value < int(schema["minimum"]):
            errors.append(f"{path}: integer below minimum {schema['minimum']}")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if isinstance(required, list):
            for key in required:
                if key not in value:
                    errors.append(f"{path}: missing required property {key}")
        if schema.get("additionalProperties") is False and isinstance(properties, dict):
            extra = sorted(set(value) - set(properties))
            for key in extra:
                errors.append(f"{path}: additional property not allowed: {key}")
        if isinstance(properties, dict):
            for key, subschema in properties.items():
                if key in value and isinstance(subschema, dict):
                    _validate_schema_node(
                        value[key], subschema, root_schema, f"{path}.{key}", errors
                    )

    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            errors.append(f"{path}: array shorter than minItems {schema['minItems']}")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            errors.append(f"{path}: array longer than maxItems {schema['maxItems']}")
        if schema.get("uniqueItems") is True:
            seen: set[str] = set()
            for item in value:
                marker = json.dumps(item, sort_keys=True)
                if marker in seen:
                    errors.append(f"{path}: duplicate item {item!r}")
                    break
                seen.add(marker)
        prefix_items = schema.get("prefixItems")
        if isinstance(prefix_items, list):
            for index, subschema in enumerate(prefix_items):
                if index < len(value) and isinstance(subschema, dict):
                    _validate_schema_node(
                        value[index], subschema, root_schema, f"{path}[{index}]", errors
                    )
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                _validate_schema_node(item, items, root_schema, f"{path}[{index}]", errors)


def validate_against_schema(
    manifest: Any,
    schema: dict[str, Any],
    *,
    source: str = "<manifest>",
) -> list[str]:
    errors: list[str] = []
    _validate_schema_node(manifest, schema, schema, source, errors)
    return errors


def _split_tokens(values: Iterable[str]) -> list[str]:
    tokens: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            tokens.extend(shlex.split(value))
        except ValueError:
            tokens.append(value)
    return tokens


def _looks_like_update_or_registry_probe(argv: list[str]) -> bool:
    tokens = [token.lower() for token in _split_tokens(argv)]
    if not tokens:
        return False
    if any(token in {"update", "upgrade", "install"} for token in tokens):
        return True
    return tokens[0] in PACKAGE_PROBE_COMMANDS and any(
        token in UPDATE_PROBE_WORDS for token in tokens[1:]
    )


def _iter_referenced_packaging_files(
    repo_root: Path,
    rel_path: str,
    source: str,
) -> tuple[list[Path], list[str]]:
    if not isinstance(rel_path, str) or not rel_path or "<" in rel_path:
        return [], []
    root = repo_root.resolve()
    path = (repo_root / rel_path).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return [], [f"{source}: packaging path escapes repo: {rel_path}"]
    if not path.exists():
        return [], []
    if path.is_file():
        return [path], []
    if path.is_dir():
        return sorted(item for item in path.rglob("*") if item.is_file()), []
    return [], []


def _scan_referenced_path_for_forbidden_args(
    repo_root: Path,
    rel_path: str,
    patterns: list[str],
    source: str,
) -> list[str]:
    files, errors = _iter_referenced_packaging_files(repo_root, rel_path, source)
    root = repo_root.resolve()
    for path in files:
        file_rel = path.relative_to(root).as_posix()
        text = path.read_text(errors="ignore")
        matches = find_forbidden_args(text.splitlines(), patterns)
        errors.extend(f"{source}: forbidden arg in {file_rel}: {match}" for match in matches)
    return errors


def validate_manifest_semantics(
    manifest: dict[str, Any],
    *,
    source: str,
    repo_root: Path | None = None,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return errors

    agent_id = manifest.get("agent_id")

    if agent_id not in EXPECTED_ADAPTERS:
        errors.append(f"{source}: unexpected agent_id {agent_id!r}")

    if source.endswith(".json") and agent_id:
        stem = Path(source).stem
        if stem != agent_id:
            errors.append(f"{source}: filename stem {stem!r} does not match agent_id")

    live_gate = manifest.get("live_gate", {})
    if live_gate.get("function") != "validate_adapter_gate" or live_gate.get("default") != "deny":
        errors.append(f"{source}: live gate must be validate_adapter_gate with default deny")

    support = manifest.get("support", {})
    readiness = manifest.get("local_readiness_state", {})
    if "controller" not in support or "worker" not in support:
        errors.append(f"{source}: controller and worker support must both be declared")
    if "controller" not in readiness or "worker" not in readiness:
        errors.append(f"{source}: controller and worker readiness must both be declared")

    if agent_id in SURVEY_WORKER_STUBS:
        controller_cap = support.get("controller", {}).get("capability")
        worker_cap = support.get("worker", {}).get("capability")
        worker_ready = readiness.get("worker")
        if controller_cap != "unsupported":
            errors.append(f"{source}: survey stub controller must be unsupported")
        if worker_cap not in {"candidate", "unsupported"}:
            errors.append(f"{source}: survey stub worker must be candidate or unsupported")
        if worker_ready != "probe_required":
            errors.append(f"{source}: survey stub worker readiness must be probe_required")
        if live_gate.get("default") != "deny":
            errors.append(f"{source}: survey stub live dispatch must default deny")

    if agent_id == "codex":
        if support.get("controller", {}).get("capability") != "supported":
            errors.append(f"{source}: codex controller capability must be supported")
        if support.get("worker", {}).get("capability") != "supported":
            errors.append(f"{source}: codex worker capability must be supported")
        if readiness.get("controller") != "ready" or readiness.get("worker") != "ready":
            errors.append(f"{source}: codex local readiness must be ready for controller and worker")
    discovery = manifest.get("discovery", {})
    budget = discovery.get("budget", {})
    probes = discovery.get("probes", [])
    if budget.get("network_default") != "deny":
        errors.append(f"{source}: discovery network_default must be deny")
    if budget.get("model_consuming") != "forbidden":
        errors.append(f"{source}: discovery model_consuming must be forbidden")

    class_counts = {"path": 0, "version": 0, "help": 0}
    for probe in probes if isinstance(probes, list) else []:
        if not isinstance(probe, dict):
            continue
        probe_id = probe.get("id", "<unnamed>")
        probe_class = probe.get("class")
        if probe_class in class_counts:
            class_counts[probe_class] += 1
        if probe.get("model_consuming") is True:
            errors.append(f"{source}: probe {probe_id} is model-consuming")
        if probe.get("network") is True:
            network = manifest.get("permission_surface", {}).get("network", {})
            if not (
                probe_class == "unsafe_user_gated"
                and probe.get("safe_for_setup") is False
                and network.get("allowed") is True
                and network.get("user_gate") is True
            ):
                errors.append(f"{source}: probe {probe_id} has undeclared network use")
        argv = probe.get("argv", [])
        if isinstance(argv, list) and _looks_like_update_or_registry_probe(argv):
            errors.append(f"{source}: probe {probe_id} is update/package-registry-like")

    for probe_class, count in class_counts.items():
        max_key = f"max_{probe_class}_probes"
        max_value = budget.get(max_key)
        if isinstance(max_value, int) and count > max_value:
            errors.append(
                f"{source}: {probe_class} probe count {count} exceeds discovery budget {max_key}={max_value}"
            )

    permission_surface = manifest.get("permission_surface", {})
    plugin_sandbox = permission_surface.get("plugin_sandbox", {})
    if plugin_sandbox.get("mode") == "broad" and not plugin_sandbox.get("declared_permissions"):
        errors.append(f"{source}: broad plugin sandbox permissions are undeclared")
    auto_approve = permission_surface.get("auto_approve_detection", {})
    if auto_approve.get("strict_fail") is not True:
        errors.append(f"{source}: auto-approve/bypass detection must strict_fail")
    if not auto_approve.get("probes"):
        errors.append(f"{source}: auto-approve/bypass detection probes must be declared")

    patterns = manifest_forbidden_patterns(manifest)
    invocation = manifest.get("invocation", {})
    exec_spec = invocation.get("exec", {})
    scan_values: list[tuple[str, list[str]]] = []
    if isinstance(exec_spec.get("args"), list):
        scan_values.append(("invocation.exec.args", exec_spec["args"]))
    if isinstance(invocation.get("commands"), list):
        scan_values.append(("invocation.commands", invocation["commands"]))
    for probe in probes if isinstance(probes, list) else []:
        if isinstance(probe, dict) and isinstance(probe.get("argv"), list):
            scan_values.append((f"discovery.probes.{probe.get('id', '<unnamed>')}.argv", probe["argv"]))
    for where, values in scan_values:
        matches = find_forbidden_args(values, patterns)
        for match in matches:
            errors.append(f"{source}: forbidden arg {match!r} in {where}")

    if repo_root is not None:
        packaging = manifest.get("packaging", {})
        if isinstance(packaging, dict):
            checked_in_wrappers = packaging.get("checked_in_wrappers", [])
            generated_outputs = packaging.get("generated_outputs", [])
            install_actions = packaging.get("install_actions", [])
            for rel_path in checked_in_wrappers if isinstance(checked_in_wrappers, list) else []:
                errors.extend(
                    _scan_referenced_path_for_forbidden_args(
                        repo_root, rel_path, patterns, f"{source}: packaging.checked_in_wrappers"
                    )
                )
            for rel_path in generated_outputs if isinstance(generated_outputs, list) else []:
                errors.extend(
                    _scan_referenced_path_for_forbidden_args(
                        repo_root,
                        rel_path,
                        patterns,
                        f"{source}: packaging.generated_outputs",
                    )
                )
            for action in install_actions if isinstance(install_actions, list) else []:
                if isinstance(action, dict):
                    errors.extend(
                        _scan_referenced_path_for_forbidden_args(
                            repo_root,
                            str(action.get("source", "")),
                            patterns,
                            f"{source}: packaging.install_actions.source",
                        )
                    )

    return errors


def validate_manifest(
    manifest: Any,
    schema: dict[str, Any],
    *,
    source: str,
    repo_root: Path | None = None,
) -> list[str]:
    return validate_against_schema(manifest, schema, source=source) + validate_manifest_semantics(
        manifest, source=source, repo_root=repo_root
    )


def _is_no_leak_allowlisted(rel: str) -> bool:
    if rel in NO_LEAK_COMPAT_ALLOWLIST:
        return True
    return any(rel.startswith(prefix) for prefix in NO_LEAK_COMPAT_PREFIXES)


def _text_for_no_leak_scan(rel: str, text: str) -> str:
    if rel != "SKILL.md" or not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    if end == -1:
        return text
    return text[end + len("\n---\n") :]


def validate_no_host_tool_leaks(repo_root: Path) -> list[str]:
    errors: list[str] = []
    suffixes = {".md", ".md.tpl", ".tpl", ".txt"}
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(repo_root).as_posix()
        if any(rel.startswith(prefix) for prefix in NO_LEAK_SKIP_PREFIXES):
            continue
        if _is_no_leak_allowlisted(rel):
            continue
        if not (path.suffix in suffixes or rel.endswith(".md.tpl")):
            continue
        text = _text_for_no_leak_scan(rel, path.read_text(errors="ignore"))
        match = HOST_TOOL_RE.search(text)
        if match:
            errors.append(f"{rel}: raw host tool leak {match.group(0)!r}")
    return errors


def validate_repository(repo_root: Path) -> tuple[int, int, list[str]]:
    adapters_dir = repo_root / "adapters"
    schema_path = adapters_dir / "agent-adapter.schema.json"
    errors: list[str] = []
    try:
        schema = json.loads(schema_path.read_text())
    except Exception as exc:  # noqa: BLE001 - CLI validator should report parse errors.
        return 0, len(EXPECTED_ADAPTERS), [f"{schema_path}: cannot load schema: {exc}"]

    manifest_paths = sorted(
        path for path in adapters_dir.glob("*.json") if path.name != "agent-adapter.schema.json"
    )
    seen = {path.stem for path in manifest_paths}
    missing = sorted(EXPECTED_ADAPTERS - seen)
    extra = sorted(seen - EXPECTED_ADAPTERS)
    for name in missing:
        errors.append(f"adapters/{name}.json: missing required adapter manifest")
    for name in extra:
        errors.append(f"adapters/{name}.json: unexpected adapter manifest")

    passed = 0
    for path in manifest_paths:
        try:
            manifest = json.loads(path.read_text())
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{path.relative_to(repo_root)}: invalid JSON: {exc}")
            continue
        source = path.relative_to(repo_root).as_posix()
        manifest_errors = validate_manifest(
            manifest, schema, source=source, repo_root=repo_root
        )
        if manifest_errors:
            errors.extend(manifest_errors)
        else:
            passed += 1

    errors.extend(validate_no_host_tool_leaks(repo_root))
    return passed, len(EXPECTED_ADAPTERS), errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        default=str(Path(__file__).resolve().parents[1]),
        help="repository root to validate",
    )
    args = parser.parse_args(argv)
    repo_root = Path(args.repo).resolve()
    passed, total, errors = validate_repository(repo_root)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        print(f"schema_validates={passed}/{total}", file=sys.stderr)
        return 1
    print(f"schema_validates={passed}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
