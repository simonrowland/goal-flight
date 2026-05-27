#!/usr/bin/env bash
# Clean OpenCode worker install: Homebrew binary + ~/.config/opencode/opencode.json
# pointed at LiteLLM frontier-reasoning. Idempotent.
set -euo pipefail

export PATH="/opt/homebrew/bin:${HOME}/.local/bin:${PATH}"

if ! command -v brew >/dev/null 2>&1; then
  echo "ERROR: Homebrew required at /opt/homebrew/bin/brew" >&2
  exit 1
fi

if ! command -v opencode >/dev/null 2>&1; then
  brew install opencode
fi

CFG_DIR="${HOME}/.config/opencode"
CFG_PATH="${CFG_DIR}/opencode.json"
mkdir -p "${CFG_DIR}"

python3 <<'PY'
import json
import os
import shutil
from pathlib import Path

home = Path.home()
cfg_dir = home / ".config/opencode"
cfg_path = cfg_dir / "opencode.json"

old: dict = {}
if cfg_path.exists():
    old = json.loads(cfg_path.read_text())

api_key = os.environ.get("LITELLM_API_KEY")
if not api_key:
    for provider in old.get("provider", {}).values():
        if not isinstance(provider, dict):
            continue
        candidate = provider.get("options", {}).get("apiKey")
        if isinstance(candidate, str) and candidate and not candidate.startswith("{env:"):
            api_key = candidate
            break

marker = "# goal-flight litellm env"
zprofile = home / ".zprofile"
if api_key:
    text = zprofile.read_text() if zprofile.exists() else ""
    if marker not in text:
        with zprofile.open("a") as handle:
            handle.write(f"\n{marker}\nexport LITELLM_API_KEY=\"{api_key}\"\n")
        print("appended LITELLM_API_KEY to ~/.zprofile")
    else:
        print("LITELLM_API_KEY marker already in ~/.zprofile")
else:
    print("WARN: LITELLM_API_KEY not set and not found in existing opencode.json", flush=True)

for name in ("node_modules", "package.json", "package-lock.json"):
    path = cfg_dir / name
    if not path.exists():
        continue
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    print(f"removed {path}")

legacy = home / ".opencode"
if legacy.exists():
    shutil.rmtree(legacy)
    print("removed legacy ~/.opencode install tree")

mcp = old.get("mcp")
if not isinstance(mcp, dict):
    npx = "/opt/homebrew/bin/npx"
    if not Path(npx).exists():
        npx = "npx"
    mcp = {
        "context-mode": {
            "command": [npx, "-y", "context-mode@latest"],
            "enabled": True,
            "type": "local",
        }
    }

config = {
    "$schema": "https://opencode.ai/config.json",
    "plugin": ["opencode-plugin-litellm@latest"],
    "model": "litellm/frontier-reasoning",
    "mcp": mcp,
    "permission": {
        "skill": {
            "goal-flight": "allow",
            "goal-flight-*": "allow",
        }
    },
    "provider": {
        "litellm": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "LiteLLM (RPP)",
            "options": {
                "baseURL": "http://rpp-ctrl:4000/v1",
                "apiKey": "{env:LITELLM_API_KEY}",
            },
        }
    },
}

cfg_path.write_text(json.dumps(config, indent=2) + "\n")
cfg_path.chmod(0o600)
print(f"wrote {cfg_path}")
print(f"model={config['model']}")
PY

echo "OpenCode binary: $(command -v opencode)"
opencode --version

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
bash "${SCRIPT_DIR}/setup_worker_path.sh"
