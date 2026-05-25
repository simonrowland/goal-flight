#!/usr/bin/env bash
# Manual live SSH dispatch smoke (Phase 2 goal 14c). Not part of ./tests/run.sh.
set -euo pipefail

if [[ "${GOALFLIGHT_LIVE_SSH:-}" != "1" ]]; then
  echo "SKIP: set GOALFLIGHT_LIVE_SSH=1 to run live fleet dispatch smoke" >&2
  exit 0
fi

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
FLEET_DIR="${GOALFLIGHT_FLEET_DIR:-$HOME/.goal-flight/fleet}"
NODE="${GOALFLIGHT_FLEET_NODE:-localhost}"
PROMPT="${GOALFLIGHT_FLEET_PROMPT:-README.md}"

cd "$REPO_ROOT"

python3 scripts/goalflight_fleet.py bootstrap "$FLEET_DIR"

echo "== preview =="
python3 scripts/goalflight_fleet.py dispatch \
  --node "$NODE" \
  --agent codex-acp \
  --billing-account openai/default \
  --prompt "$PROMPT" \
  --thin-defaults \
  --json | head -40

echo "== exec (live SSH) =="
python3 scripts/goalflight_fleet.py dispatch \
  --node "$NODE" \
  --agent codex-acp \
  --billing-account openai/default \
  --prompt "$PROMPT" \
  --exec \
  --json

echo "== watch once =="
python3 scripts/goalflight_fleet.py watch --fleet --once --json

echo "== reconcile =="
python3 scripts/goalflight_fleet.py reconcile --all-in-flight --json

echo "OK: live fleet dispatch smoke completed"
