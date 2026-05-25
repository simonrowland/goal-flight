#!/usr/bin/env bash
# Manual live SSH dispatch smoke (Phase 2 goal 14c). Not part of ./tests/run.sh.
set -euo pipefail

if [[ "${GOALFLIGHT_LIVE_SSH:-}" != "1" ]]; then
  echo "SKIP: set GOALFLIGHT_LIVE_SSH=1 to run live fleet dispatch smoke" >&2
  exit 0
fi

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export GOALFLIGHT_FLEET_DIR="${GOALFLIGHT_FLEET_DIR:-$HOME/.goal-flight/fleet}"
SSH_ALIAS="${GOALFLIGHT_FLEET_SSH_ALIAS:-${GOALFLIGHT_FLEET_NODE:-localhost}}"
NODE="${GOALFLIGHT_FLEET_NODE:-$SSH_ALIAS}"
PROMPT="${GOALFLIGHT_FLEET_PROMPT:-README.md}"
STATE_DIR="${GOALFLIGHT_FLEET_STATE_DIR:-$HOME/.goal-flight}"
BILLING="${GOALFLIGHT_FLEET_BILLING:-openai/default}"
FLEET=(python3 scripts/goalflight_fleet.py --fleet-dir "$GOALFLIGHT_FLEET_DIR")

cd "$REPO_ROOT"

node_registered() {
  python3 - "$GOALFLIGHT_FLEET_DIR" "$NODE" <<'PY'
import json
import sys
from pathlib import Path

fleet_dir = Path(sys.argv[1])
node_id = sys.argv[2]
doc = json.loads((fleet_dir / "fleet.json").read_text())
sys.exit(0 if node_id in (doc.get("nodes") or {}) else 1)
PY
}

preflight_ssh() {
  local target="$1"
  echo "== preflight: ssh $target =="
  if ! ssh -o BatchMode=yes -o ConnectTimeout=5 "$target" echo goal-flight-probe-ok >/dev/null 2>&1; then
    cat >&2 <<EOF
ERROR: passwordless SSH to '$target' failed.

Loopback smoke needs Remote Login + key auth to the same repo path:
  $REPO_ROOT

Typical fix on macOS:
  1. System Settings → General → Sharing → Remote Login ON
  2. Add a ~/.ssh/config Host (or use GOALFLIGHT_FLEET_SSH_ALIAS):
       Host localhost
         HostName 127.0.0.1
         User $(whoami)
         IdentityFile ~/.ssh/id_ed25519
  3. ssh-copy-id localhost   # or add your pubkey to ~/.ssh/authorized_keys
  4. ssh localhost echo ok

Or point at an existing Host alias:
  export GOALFLIGHT_FLEET_SSH_ALIAS=rpp-ctrl
  export GOALFLIGHT_FLEET_NODE=rpp-ctrl
  export GOALFLIGHT_FLEET_REPO_ROOT=/path/on/remote
EOF
    exit 1
  fi
  echo "ssh ok"
}

ensure_node() {
  if node_registered; then
    echo "== node already registered: $NODE =="
    return 0
  fi
  echo "== onboarding node $NODE (ssh alias $SSH_ALIAS) =="
  "${FLEET[@]}" node add \
    --from-ssh "$SSH_ALIAS" \
    --node-id "$NODE" \
    --repo-root "${GOALFLIGHT_FLEET_REPO_ROOT:-$REPO_ROOT}" \
    --state-dir "$STATE_DIR" \
    --billing-accounts "$BILLING" \
    --json
  echo "== linking billing account =="
  "${FLEET[@]}" account link \
    --account-key "$BILLING" \
    --node "$NODE" \
    --json
}

"${FLEET[@]}" bootstrap
preflight_ssh "$SSH_ALIAS"
ensure_node

echo "== preview =="
"${FLEET[@]}" dispatch \
  --node "$NODE" \
  --agent codex-acp \
  --billing-account "$BILLING" \
  --prompt "$PROMPT" \
  --thin-defaults \
  --json | head -40

echo "== exec (live SSH) =="
"${FLEET[@]}" dispatch \
  --node "$NODE" \
  --agent codex-acp \
  --billing-account "$BILLING" \
  --prompt "$PROMPT" \
  --exec \
  --json

echo "== watch once =="
"${FLEET[@]}" watch --fleet --once --json

echo "== reconcile =="
"${FLEET[@]}" reconcile --all-in-flight --json

echo "OK: live fleet dispatch smoke completed"
