#!/usr/bin/env bash
# Live controller behavior scenarios via Claude Code ACP shim.
#
# Skips when:
#   - claude-code-cli-acp not installed
#   - GOALFLIGHT_CONTROLLER_BEHAVIOR is unset (keeps default ./tests/run.sh fast)

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
RUNNER="$REPO_ROOT/scripts/hosts/controller/behavior_scenario.py"
SELF="tests/bash/test-controller-behavior-claude-code-acp.sh"

if [ -z "${GOALFLIGHT_CONTROLLER_BEHAVIOR:-}" ]; then
  echo "SKIP  $SELF (set GOALFLIGHT_CONTROLLER_BEHAVIOR=1)"
  exit 0
fi

PYTHON_BIN="$(command -v python3 || true)"
if [ -z "$PYTHON_BIN" ]; then
  echo "SKIP  $SELF (python3 missing)"
  exit 0
fi

ACP_BIN="$(command -v claude-code-cli-acp || true)"
if [ -z "$ACP_BIN" ]; then
  echo "SKIP  $SELF (claude-code-cli-acp not installed)"
  exit 0
fi

TRANSCRIPT_DIR="${GOALFLIGHT_CONTROLLER_TRANSCRIPT_DIR:-$REPO_ROOT/docs-private/reviews/$(date +%F)-chunk-15}"
mkdir -p "$TRANSCRIPT_DIR"

SCENARIOS="${GOALFLIGHT_CONTROLLER_SCENARIOS:-doctor-loads resume-after-compaction continue-prescribed-step-two read-skill-end-to-end compaction-reload-skill review-flight-at-completion chat-as-requirements draft-goal-office-hours vague-goal-premise-backlog context-load-order goal-loop-default dispatch-cli-worker-via-crash-safe-command never-pgrep-for-worker-liveness no-hand-iterate}"
FAIL=0

for SCENARIO in $SCENARIOS; do
  JSON_OUT="/tmp/controller-behavior-claude-code-acp-${SCENARIO}-$$.json"
  ERR_OUT="/tmp/controller-behavior-claude-code-acp-${SCENARIO}-$$.err"
  if ! "$PYTHON_BIN" "$RUNNER" \
    --controller claude-acp \
    --scenario "$SCENARIO" \
    --directory "$REPO_ROOT" \
    --transcript-dir "$TRANSCRIPT_DIR" \
    --json \
    > "$JSON_OUT" 2>"$ERR_OUT"; then
    if "$PYTHON_BIN" - <<'PY' "$JSON_OUT"
import json, sys
payload = json.load(open(sys.argv[1]))
sys.exit(0 if payload.get("skipped") else 1)
PY
    then
      echo "SKIP  scenario $SCENARIO (harness skipped)"
      continue
    fi
    echo "FAIL  scenario $SCENARIO"
    cat "$ERR_OUT" | sed 's/^/      /'
    cat "$JSON_OUT" | sed 's/^/      /'
    FAIL=1
    continue
  fi

  if ! "$PYTHON_BIN" - <<'PY' "$JSON_OUT" "$SCENARIO"
import json, sys
payload = json.load(open(sys.argv[1]))
scenario = sys.argv[2]
if payload.get("skipped"):
    print(f"SKIP  scenario {scenario}")
    sys.exit(0)
assert payload.get("ok"), payload
for check in payload.get("checks") or []:
    assert check.get("ok"), check
session = payload.get("session") or {}
transcript = session.get("transcript_path")
assert transcript, payload
print(f"PASS  scenario {scenario} transcript={transcript}")
PY
  then
    FAIL=1
  fi
done

if [ "$FAIL" -ne 0 ]; then
  echo "FAIL  $SELF"
  exit 1
fi

echo "PASS  $SELF"
