#!/usr/bin/env bash
# Live Codex controller behavior scenarios (bash-tail).
#
# Skips when:
#   - codex not installed
#   - GOALFLIGHT_CONTROLLER_BEHAVIOR is unset (keeps default ./tests/run.sh fast)

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
RUNNER="$REPO_ROOT/scripts/hosts/controller/behavior_scenario.py"

if [ -z "${GOALFLIGHT_CONTROLLER_BEHAVIOR:-}" ]; then
  echo "SKIP  tests/bash/test-controller-behavior-codex.sh (set GOALFLIGHT_CONTROLLER_BEHAVIOR=1)"
  exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "SKIP  tests/bash/test-controller-behavior-codex.sh (python3 missing)"
  exit 0
fi

if ! command -v codex >/dev/null 2>&1; then
  echo "SKIP  tests/bash/test-controller-behavior-codex.sh (codex not installed)"
  exit 0
fi

SCENARIOS="${GOALFLIGHT_CONTROLLER_SCENARIOS:-doctor-loads resume-after-compaction continue-prescribed-step-two read-skill-end-to-end compaction-reload-skill review-flight-at-completion}"
FAIL=0

for SCENARIO in $SCENARIOS; do
  if ! python3 "$RUNNER" --controller codex --scenario "$SCENARIO" --directory "$REPO_ROOT" --json \
    > "/tmp/controller-behavior-codex-${SCENARIO}-$$.json" 2>"/tmp/controller-behavior-codex-${SCENARIO}-$$.err"; then
    if python3 - <<'PY' "/tmp/controller-behavior-codex-${SCENARIO}-$$.json"
import json, sys
payload = json.load(open(sys.argv[1]))
sys.exit(0 if payload.get("skipped") else 1)
PY
    then
      echo "SKIP  scenario $SCENARIO (harness skipped)"
      continue
    fi
    echo "FAIL  scenario $SCENARIO"
    cat "/tmp/controller-behavior-codex-${SCENARIO}-$$.err" | sed 's/^/      /'
    cat "/tmp/controller-behavior-codex-${SCENARIO}-$$.json" | sed 's/^/      /'
    FAIL=1
    continue
  fi

  if ! python3 - <<'PY' "/tmp/controller-behavior-codex-${SCENARIO}-$$.json" "$SCENARIO"
import json, sys
payload = json.load(open(sys.argv[1]))
scenario = sys.argv[2]
if payload.get("skipped"):
    print(f"SKIP  scenario {scenario}")
    sys.exit(0)
assert payload.get("ok"), payload
for check in payload.get("checks") or []:
    assert check.get("ok"), check
print(f"PASS  scenario {scenario}")
PY
  then
    FAIL=1
  fi
done

if [ "$FAIL" -ne 0 ]; then
  echo "FAIL  tests/bash/test-controller-behavior-codex.sh"
  exit 1
fi

echo "PASS  tests/bash/test-controller-behavior-codex.sh"
