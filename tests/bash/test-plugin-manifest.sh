#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VERSION_FILE="$REPO_ROOT/VERSION"
CLAUDE_MANIFEST="$REPO_ROOT/.claude-plugin/plugin.json"
CODEX_PLUGIN_MANIFEST="$REPO_ROOT/plugins/goal-flight/.codex-plugin/plugin.json"
CODEX_MARKETPLACE="$REPO_ROOT/.agents/plugins/marketplace.json"
CODEX_PLUGIN_SKILLS="$REPO_ROOT/plugins/goal-flight/skills"

for forbidden in \
  "$REPO_ROOT/.codex-plugin/plugin.json" \
  "$REPO_ROOT/skills/goal-flight/SKILL.md" \
  "$REPO_ROOT/skills/goal-flight-doctor/SKILL.md" \
  "$REPO_ROOT/skills/goal-flight-init/SKILL.md"
do
  [ ! -e "$forbidden" ] || {
    echo "duplicate root Codex package path must not exist: $forbidden" >&2
    exit 1
  }
done

[ -f "$CLAUDE_MANIFEST" ] || {
  echo "missing .claude-plugin/plugin.json" >&2
  exit 1
}

python3 - "$CLAUDE_MANIFEST" "$CODEX_PLUGIN_MANIFEST" "$CODEX_MARKETPLACE" "$VERSION_FILE" <<'PY'
import json
import sys
from pathlib import Path

manifest_paths = [Path(value) for value in sys.argv[1:3]]
marketplace_path = Path(sys.argv[3])
version_path = Path(sys.argv[4])
version = version_path.read_text().strip()

for manifest_path in manifest_paths:
    if not manifest_path.exists():
        raise SystemExit(f"missing manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text())
    required = ["name", "version", "description", "author", "homepage", "repository", "license"]
    missing = [key for key in required if not manifest.get(key)]
    if missing:
        raise SystemExit(f"{manifest_path}: missing manifest keys: {', '.join(missing)}")
    if manifest["name"] != "goal-flight":
        raise SystemExit(f"{manifest_path}: unexpected plugin name: {manifest['name']}")
    if manifest["version"] != version:
        raise SystemExit(f"{manifest_path}: manifest version {manifest['version']} != VERSION {version}")

marketplace = json.loads(marketplace_path.read_text())
plugins = marketplace.get("plugins") or []
if marketplace.get("name") != "goal-flight":
    raise SystemExit("codex marketplace name mismatch")
if not any(plugin.get("name") == "goal-flight" and plugin.get("source", {}).get("path") == "./plugins/goal-flight" for plugin in plugins):
    raise SystemExit("codex marketplace missing goal-flight local plugin path")
PY

for skill in goal-flight goal-flight-doctor goal-flight-init; do
  skill_file="$CODEX_PLUGIN_SKILLS/$skill/SKILL.md"
  [ -f "$skill_file" ] || {
    echo "missing Codex skill wrapper: $skill_file" >&2
    exit 1
  }
  grep -q "name: $skill" "$skill_file" || {
    echo "Codex skill wrapper name mismatch: $skill_file" >&2
    exit 1
  }
done

if command -v claude >/dev/null 2>&1; then
  claude plugin validate "$REPO_ROOT" >/tmp/goal-flight-plugin-validate-$$.out 2>&1 || {
    cat /tmp/goal-flight-plugin-validate-$$.out >&2
    rm -f /tmp/goal-flight-plugin-validate-$$.out
    exit 1
  }
  rm -f /tmp/goal-flight-plugin-validate-$$.out
fi

DOCTOR="$REPO_ROOT/commands/doctor.md"
for expected in \
  "/Applications/Cursor.app" \
  "/Applications/Codex.app" \
  "Codex Desktop found, but codex CLI missing" \
  "npm install -g @openai/codex && codex login" \
  "command -v cursor" \
  "command -v cursor-agent" \
  "Grok Build" \
  "--prompt-file" \
  "--permission-mode" \
  "--os-sandbox"
do
  grep -q -- "$expected" "$DOCTOR" || {
    echo "doctor.md missing expected probe: $expected" >&2
    exit 1
  }
done
