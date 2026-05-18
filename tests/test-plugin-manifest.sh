#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MANIFEST="$REPO_ROOT/.claude-plugin/plugin.json"
VERSION_FILE="$REPO_ROOT/VERSION"

[ -f "$MANIFEST" ] || {
  echo "missing .claude-plugin/plugin.json" >&2
  exit 1
}

python3 - "$MANIFEST" "$VERSION_FILE" <<'PY'
import json
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
version_path = Path(sys.argv[2])
manifest = json.loads(manifest_path.read_text())
version = version_path.read_text().strip()

required = ["name", "version", "description", "author", "homepage", "repository", "license"]
missing = [key for key in required if not manifest.get(key)]
if missing:
    raise SystemExit(f"missing manifest keys: {', '.join(missing)}")

if manifest["name"] != "goal-flight":
    raise SystemExit(f"unexpected plugin name: {manifest['name']}")

if manifest["version"] != version:
    raise SystemExit(f"manifest version {manifest['version']} != VERSION {version}")
PY

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
  "--permission-mode"
do
  grep -q -- "$expected" "$DOCTOR" || {
    echo "doctor.md missing expected probe: $expected" >&2
    exit 1
  }
done
