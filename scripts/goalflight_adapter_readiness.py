#!/usr/bin/env python3
"""Machine-local readiness helpers for live adapter dispatch."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

from goalflight_adapter_gate import validate_adapter_gate
from goalflight_os_sandbox import (
    OS_SANDBOX_OFF,
    os_sandbox_platform_key,
    platform_supported_os_sandbox_profiles,
)


SCRIPT_DIR = Path(__file__).resolve().parent
ADAPTERS_DIR = Path(os.environ.get("GOALFLIGHT_ADAPTERS_DIR", SCRIPT_DIR.parent / "adapters"))
AGENT_MANIFEST_ALIASES = {
    "claude-acp": "claude-code",
    "claude": "claude-code",
    "codex-acp": "codex",
    "cursor-agent": "cursor",
}


def manifest_candidates(agent: str) -> list[Path]:
    names = [agent, AGENT_MANIFEST_ALIASES.get(agent)]
    if "/" in agent:
        names.append(Path(agent).stem)
    return [ADAPTERS_DIR / f"{name}.json" for name in names if name]


def load_manifest(agent: str) -> dict[str, Any] | None:
    manifest, _ = load_manifest_with_reason(agent)
    return manifest


def load_manifest_with_reason(agent: str) -> tuple[dict[str, Any] | None, str | None]:
    for path in manifest_candidates(agent):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except OSError:
            return None, "adapter_manifest_unreadable"
        except json.JSONDecodeError:
            return None, "adapter_manifest_invalid"
        if not isinstance(data, dict):
            return None, "adapter_manifest_invalid"
        return data, None
    return None, "adapter_manifest_missing"


def _probe_env(probe: dict[str, Any]) -> dict[str, str] | None:
    if not probe.get("env_scrub"):
        return None
    path_items = [
        str(Path.home() / ".local/bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    return {
        "HOME": str(Path.home()),
        "PATH": os.pathsep.join(path_items),
        "SHELL": os.environ.get("SHELL", "/bin/sh"),
    }


def _run_probe(probe: dict[str, Any]) -> bool:
    argv = probe.get("argv")
    if not isinstance(argv, list) or not all(isinstance(part, str) for part in argv):
        return False
    if not probe.get("safe_for_setup") or probe.get("network") or probe.get("model_consuming"):
        return False
    try:
        result = subprocess.run(
            argv,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=5,
            check=False,
            env=_probe_env(probe),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def local_readiness_state(manifest: dict[str, Any]) -> dict[str, Any]:
    required_ids = manifest.get("local_readiness_state", {}).get("last_probe_ids", [])
    required = {str(item) for item in required_ids if isinstance(item, str)}
    probes = {
        str(probe.get("id")): probe
        for probe in manifest.get("discovery", {}).get("probes", [])
        if isinstance(probe, dict) and probe.get("id")
    }
    ready = bool(required) and all(
        probe_id in probes and _run_probe(probes[probe_id]) for probe_id in required
    )
    state = "ready" if ready else "probe_required"
    return {
        "controller": state,
        "worker": state,
        "last_probe_ids": sorted(required),
    }


def _command_available(argv: list[str]) -> bool:
    if not argv:
        return False
    binary = argv[0]
    if "/" in binary:
        path = Path(binary).expanduser()
        return path.exists() and os.access(path, os.X_OK)
    return shutil.which(binary) is not None


def validate_acp_dispatch_readiness(agent: str, argv: list[str]) -> dict[str, Any] | None:
    manifest, manifest_error = load_manifest_with_reason(agent)
    if manifest is None:
        return {
            "allowed": False,
            "reason": manifest_error or "adapter_manifest_missing",
            "required_probe_ids": [],
            "blocked_fields": ["adapter"],
            "safe_next_action": "fix_adapter_manifest_or_select_known_adapter",
            "live_controller_allowed": False,
            "live_worker_dispatch_allowed": False,
            "live_entry": "acp_dispatch",
        }
    if not _command_available(argv):
        return {
            "allowed": False,
            "reason": "not_installed",
            "required_probe_ids": [],
            "blocked_fields": ["argv[0]"],
            "safe_next_action": "install_or_select_another_adapter",
            "live_controller_allowed": False,
            "live_worker_dispatch_allowed": False,
            "live_entry": "acp_dispatch",
        }
    decision = validate_adapter_gate(
        manifest,
        role="worker",
        requested_transport="acp",
        argv=argv,
        live_entry="acp_dispatch",
        local_state=local_readiness_state(manifest),
    )
    return None if decision.get("allowed") else decision


def validate_os_sandbox_request(agent: str, profile: str | None) -> dict[str, Any] | None:
    if profile in {None, OS_SANDBOX_OFF}:
        return None
    platform_key = os_sandbox_platform_key()
    platform_supported = platform_supported_os_sandbox_profiles()
    manifest = load_manifest(agent) or {}
    os_spec = ((manifest.get("permission_surface") or {}).get("os_sandbox") or {})
    if not isinstance(os_spec, dict):
        return {
            "allowed": False,
            "reason": "os_sandbox_undeclared",
            "profile": profile,
            "safe_next_action": "declare_os_sandbox_support_or_use_off",
        }
    supported = os_spec.get("supported_profiles")
    if not isinstance(supported, list) or profile not in supported:
        return {
            "allowed": False,
            "reason": "os_sandbox_unsupported",
            "profile": profile,
            "supported_profiles": supported if isinstance(supported, list) else [],
            "safe_next_action": "select_supported_os_sandbox_profile",
        }
    platform_scoped = os_spec.get("platform_supported_profiles")
    if isinstance(platform_scoped, dict):
        scoped_supported = platform_scoped.get(platform_key)
        if scoped_supported is None and platform_key == "wsl":
            scoped_supported = platform_scoped.get("linux")
        if not isinstance(scoped_supported, list) or profile not in scoped_supported:
            return {
                "allowed": False,
                "reason": "os_sandbox_platform_unsupported",
                "profile": profile,
                "platform": platform_key,
                "supported_profiles": scoped_supported if isinstance(scoped_supported, list) else [],
                "safe_next_action": "use_os_sandbox_off_on_this_platform",
            }
    if profile not in platform_supported:
        return {
            "allowed": False,
            "reason": "os_sandbox_platform_unsupported",
            "profile": profile,
            "platform": platform_key,
            "supported_profiles": platform_supported,
            "safe_next_action": "use_os_sandbox_off_on_this_platform",
        }
    return None
