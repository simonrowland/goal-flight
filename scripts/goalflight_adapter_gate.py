#!/usr/bin/env python3
"""Deny-by-default static live gate for Goal Flight adapter manifests."""

from __future__ import annotations

import fnmatch
import json
import shlex
from pathlib import Path
from typing import Any, Iterable


DEFAULT_FORBIDDEN_ARG_PATTERNS = (
    "--yolo",
    "-y",
    "--dangerously-*",
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-skip-permissions",
    "--dangerously-allow-all",
    "--allow-dangerously-skip-permissions",
    "--always-approve",
    "--allow-all-tools",
    "--trust-all-tools",
    "--auto-approve",
    "--auto-accept",
    "--skip-permissions",
    "--no-sandbox",
    "--disable-sandbox",
    "--sandbox-disable",
)

REQUIRED_GATE_CONTRACT_SECTIONS = (
    "support",
    "local_readiness_state",
    "live_gate",
    "status_contract",
    "permission_surface",
)

GATE_NEXT_ACTIONS = {
    "allowed": "dispatch_allowed",
    "unsupported": "choose_supported_adapter",
    "config_only": "use_static_config_only",
    "candidate": "promote_manifest_after_probe_evidence",
    "probe_required": "run_explicit_safe_probe_before_dispatch",
    "not_installed": "install_or_select_another_adapter",
    "forbidden-arg": "remove_forbidden_argument",
    "failed": "inspect_failed_readiness_probe",
    "blocked": "resolve_blocker_before_dispatch",
}


def _load_adapter(adapter: Any) -> Any:
    if isinstance(adapter, (str, Path)):
        return json.loads(Path(adapter).read_text())
    return adapter


def _tokenize_args(values: Iterable[str]) -> list[str]:
    tokens: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            tokens.extend(shlex.split(value))
        except ValueError:
            tokens.append(value)
    return tokens


def find_forbidden_args(
    argv: Iterable[str] | None,
    forbidden_patterns: Iterable[str] = DEFAULT_FORBIDDEN_ARG_PATTERNS,
) -> list[str]:
    """Return forbidden argv tokens matched by exact or shell-style patterns."""

    if argv is None:
        return []
    patterns = list(forbidden_patterns)
    matches: list[str] = []
    for token in _tokenize_args(str(item) for item in argv):
        candidates = [token]
        if "=" in token:
            candidates.append(token.split("=", 1)[0])
        for pattern in patterns:
            if any(
                candidate == pattern or fnmatch.fnmatchcase(candidate, pattern)
                for candidate in candidates
            ):
                matches.append(token)
                break
    return matches


def manifest_forbidden_patterns(adapter: dict[str, Any]) -> list[str]:
    patterns = list(DEFAULT_FORBIDDEN_ARG_PATTERNS)
    if not isinstance(adapter, dict):
        return patterns
    declared = (
        adapter.get("invocation", {})
        .get("exec", {})
        .get("arg_policy", {})
        .get("forbidden_args", [])
    )
    if isinstance(declared, list):
        for item in declared:
            if isinstance(item, str) and item not in patterns:
                patterns.append(item)
    return patterns


def manifest_gate_contract(manifest: Any) -> list[str]:
    """Return gate-local deny-by-default contract violations."""

    if not isinstance(manifest, dict):
        return ["manifest"]

    errors: list[str] = []
    for section in REQUIRED_GATE_CONTRACT_SECTIONS:
        value = manifest.get(section)
        if not isinstance(value, dict):
            errors.append(section)

    live_gate = manifest.get("live_gate")
    if isinstance(live_gate, dict):
        if live_gate.get("function") != "validate_adapter_gate":
            errors.append("live_gate.function")
        if live_gate.get("default") != "deny":
            errors.append("live_gate.default")

    status_contract = manifest.get("status_contract")
    if isinstance(status_contract, dict):
        for key in ("terminal_states", "stale_after_s"):
            if key not in status_contract:
                errors.append(f"status_contract.{key}")

    permission_surface = manifest.get("permission_surface")
    if isinstance(permission_surface, dict):
        for key in ("plugin_sandbox", "auto_approve_detection"):
            if not isinstance(permission_surface.get(key), dict):
                errors.append(f"permission_surface.{key}")
        auto_approve = permission_surface.get("auto_approve_detection")
        if isinstance(auto_approve, dict) and auto_approve.get("strict_fail") is not True:
            errors.append("permission_surface.auto_approve_detection.strict_fail")

    return errors


def _probe_ids(adapter: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    readiness_ids = adapter.get("local_readiness_state", {}).get("last_probe_ids", [])
    if isinstance(readiness_ids, list):
        ids.extend(str(item) for item in readiness_ids)
    for probe in adapter.get("discovery", {}).get("probes", []):
        if isinstance(probe, dict) and probe.get("id") not in ids:
            ids.append(str(probe.get("id")))
    return ids


def _readiness_reason(state: str) -> str:
    if state == "not_installed":
        return "not_installed"
    if state == "unsupported":
        return "unsupported"
    if state in {"probe_required", "failed", "blocked"}:
        return state
    return "probe_required"


def _role_decision(
    adapter: dict[str, Any],
    role: str,
    requested_transport: str | None,
    forbidden: list[str],
) -> tuple[bool, str, list[str]]:
    support = adapter.get("support", {}).get(role, {})
    readiness = adapter.get("local_readiness_state", {}).get(role)
    capability = support.get("capability")
    fallback = support.get("fallback")
    blocked_fields: list[str] = []

    if forbidden:
        return False, "forbidden-arg", ["argv"]

    if role not in {"controller", "worker"}:
        return False, "unsupported", ["role"]

    if capability == "candidate":
        return False, "candidate", [f"support.{role}.capability"]

    if capability == "unsupported":
        reason = "config_only" if fallback == "config_only" else "unsupported"
        return False, reason, [f"support.{role}.capability"]

    if capability != "supported":
        return False, "unsupported", [f"support.{role}.capability"]

    if readiness != "ready":
        return False, _readiness_reason(str(readiness)), [f"local_readiness_state.{role}"]

    if role == "worker":
        transports = support.get("transport", [])
        if requested_transport not in transports:
            return False, "probe_required", ["requested_transport"]

    return True, "allowed", blocked_fields


def validate_adapter_gate(
    adapter: Any,
    *,
    role: str,
    requested_transport: str | None = None,
    argv: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return gate decision. Unknown or incomplete input is denied.

    A true decision requires:
    - matching supported static capability;
    - matching machine-local readiness = ready;
    - worker transport explicitly requested and declared;
    - no forbidden/bypass argument in argv.
    """

    manifest = _load_adapter(adapter)
    contract_errors = manifest_gate_contract(manifest)
    if contract_errors:
        return {
            "allowed": False,
            "reason": "unsupported",
            "required_probe_ids": [],
            "blocked_fields": contract_errors,
            "safe_next_action": "fix_adapter_manifest_contract",
            "live_controller_allowed": False,
            "live_worker_dispatch_allowed": False,
        }

    forbidden = find_forbidden_args(argv, manifest_forbidden_patterns(manifest))
    allowed, reason, blocked_fields = _role_decision(
        manifest, role, requested_transport, forbidden
    )
    controller_allowed, _, _ = _role_decision(manifest, "controller", None, forbidden)
    worker_allowed, _, _ = _role_decision(
        manifest, "worker", requested_transport, forbidden
    )

    required_probe_ids: list[str] = []
    if not allowed and reason in {
        "probe_required",
        "not_installed",
        "failed",
        "blocked",
        "candidate",
    }:
        required_probe_ids = _probe_ids(manifest)

    return {
        "allowed": bool(allowed),
        "reason": reason,
        "required_probe_ids": required_probe_ids,
        "blocked_fields": blocked_fields,
        "safe_next_action": GATE_NEXT_ACTIONS.get(reason, "deny"),
        "live_controller_allowed": bool(controller_allowed),
        "live_worker_dispatch_allowed": bool(worker_allowed),
    }


__all__ = [
    "DEFAULT_FORBIDDEN_ARG_PATTERNS",
    "find_forbidden_args",
    "manifest_forbidden_patterns",
    "manifest_gate_contract",
    "validate_adapter_gate",
]
