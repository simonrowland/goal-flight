#!/usr/bin/env bash
# Hermetic + live probe matrix smoke for controller harness.

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PROBE="$REPO_ROOT/scripts/hosts/controller/probe_matrix.py"

if ! command -v python3 >/dev/null 2>&1; then
  echo "SKIP  tests/bash/test-controller-probe-matrix.sh (python3 missing)"
  exit 0
fi

if ! python3 "$REPO_ROOT/tests/python/test_controller_probe_matrix.py" > /tmp/controller-probe-matrix-$$.out 2>&1; then
  echo "FAIL  tests/bash/test-controller-probe-matrix.sh (hermetic python)"
  cat /tmp/controller-probe-matrix-$$.out | sed 's/^/      /'
  exit 1
fi

python3 "$PROBE" --json > /tmp/controller-probe-live-$$.json
python3 - <<'PY' /tmp/controller-probe-live-$$.json
import json, sys
payload = json.load(open(sys.argv[1]))
avail = payload.get("available_controllers") or []
print(f"INFO  available controllers: {', '.join(avail) if avail else '(none)'}")
PY

echo "PASS  tests/bash/test-controller-probe-matrix.sh"
