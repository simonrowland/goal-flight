"""OpenCode LiteLLM install profile detection and merge helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


HOST_DIR = Path(__file__).resolve().parent
REPO_ROOT = HOST_DIR.parents[2]
LITELLM_EXAMPLE_PATH = REPO_ROOT / "configs/opencode/litellm.example.json"
LITELLM_LOCAL_PATHS = (
    REPO_ROOT / "configs/opencode/litellm.local.json",
    Path.home() / ".config/goal-flight/opencode-litellm.json",
)
LITELLM_ENV_PATHS = (
    Path.home() / ".config/goal-flight/litellm.env",
    Path.home() / ".config/rpp/litellm.env",
)


def load_litellm_env() -> None:
    """Load LiteLLM credentials from env files without overwriting existing env."""
    if os.environ.get("LITELLM_API_KEY") or os.environ.get("LITELLM_MASTER_KEY"):
        _sync_litellm_api_key_alias()
        return
    for env_file in LITELLM_ENV_PATHS:
        if not env_file.is_file():
            continue
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export ") :].strip()
            value = value.strip().strip("'\"")
            os.environ.setdefault(key, value)
        if os.environ.get("LITELLM_API_KEY") or os.environ.get("LITELLM_MASTER_KEY"):
            _sync_litellm_api_key_alias()
            return
    _sync_litellm_api_key_alias()


def _sync_litellm_api_key_alias() -> None:
    """OpenCode litellm provider reads LITELLM_API_KEY; accept MASTER_KEY as alias."""
    if os.environ.get("LITELLM_API_KEY"):
        return
    master = os.environ.get("LITELLM_MASTER_KEY", "").strip()
    if master:
        os.environ.setdefault("LITELLM_API_KEY", master)


def litellm_credentials_available() -> bool:
    load_litellm_env()
    return bool(os.environ.get("LITELLM_API_KEY") or os.environ.get("LITELLM_MASTER_KEY"))


def litellm_profile_path() -> Path | None:
    for path in LITELLM_LOCAL_PATHS:
        if path.is_file():
            return path
    if LITELLM_EXAMPLE_PATH.is_file():
        return LITELLM_EXAMPLE_PATH
    return None


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def configured_model(data: dict[str, Any]) -> str | None:
    model = data.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    return None


def existing_litellm_provider(data: dict[str, Any]) -> bool:
    provider = data.get("provider")
    return isinstance(provider, dict) and isinstance(provider.get("litellm"), dict)


def should_wire_litellm(existing: dict[str, Any]) -> bool:
    if any(path.is_file() for path in LITELLM_LOCAL_PATHS):
        return True
    if litellm_credentials_available():
        return True
    if existing_litellm_provider(existing):
        return True
    return False


def load_litellm_profile() -> dict[str, Any]:
    path = litellm_profile_path()
    if path is None:
        return {}
    return dict(_load_json_object(path))


def litellm_install_overlay(existing: dict[str, Any]) -> dict[str, Any]:
    """Return LiteLLM provider/plugin overlay, or {} when LiteLLM should stay untouched."""
    if not should_wire_litellm(existing):
        return {}
    profile = load_litellm_profile()
    if not profile:
        return {}
    if configured_model(existing):
        profile = {key: value for key, value in profile.items() if key != "model"}
    env_model = os.environ.get("LITELLM_OPENCODE_MODEL", "").strip()
    if env_model and not configured_model(existing):
        profile["model"] = env_model
    return profile
