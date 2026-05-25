#!/usr/bin/env bash
# Manual C1 partition checklist (Phase 2 goal 16b). Not part of ./tests/run.sh.
set -euo pipefail

if [[ "${GOALFLIGHT_LIVE_SSH:-}" != "1" ]]; then
  echo "SKIP: set GOALFLIGHT_LIVE_SSH=1 to run live C1 partition harness" >&2
  exit 0
fi

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
FLEET_DIR="${GOALFLIGHT_FLEET_DIR:-$HOME/.goal-flight/fleet}"
DISPATCH_ID="${GOALFLIGHT_FLEET_DISPATCH_ID:-}"

cd "$REPO_ROOT"

echo "== C1 live partition harness (operator-assisted) =="
echo "Fleet dir: $FLEET_DIR"
echo "Runbook: docs-private/runbooks/fleet-c1-live-partition.md"
echo

if [[ -z "$DISPATCH_ID" ]]; then
  echo "No GOALFLIGHT_FLEET_DISPATCH_ID set."
  echo "Start a live dispatch first, then re-run with:"
  echo "  export GOALFLIGHT_FLEET_DISPATCH_ID=<dispatch_id>"
  echo
  echo "Checklist only (no automated assertions without dispatch id)."
  exit 0
fi

echo "Dispatch under test: $DISPATCH_ID"
echo

echo "== Scenario 1 prep: reconcile while SSH may be down =="
python3 scripts/goalflight_fleet.py reconcile --dispatch-id "$DISPATCH_ID" --json | head -30

echo
echo "== Watch once (refresh mirrors) =="
python3 scripts/goalflight_fleet.py watch --fleet --once --json | head -30

echo
echo "== Scenario 2/3: reconcile again (idempotency check) =="
python3 scripts/goalflight_fleet.py reconcile --dispatch-id "$DISPATCH_ID" --json | head -30

echo
echo "OK: C1 harness finished — compare JSON released flags against runbook expectations"
