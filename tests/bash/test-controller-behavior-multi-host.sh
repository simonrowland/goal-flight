#!/usr/bin/env bash
# Live orchestrator behavior scenarios across env-gated available hosts.
#
# Skips unless GOALFLIGHT_LIVE_CONTROLLERS is set to a comma-separated host list.

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
RUNNER="$REPO_ROOT/scripts/hosts/controller/behavior_scenario.py"
PROBE="$REPO_ROOT/scripts/hosts/controller/probe_matrix.py"
SELF="tests/bash/test-controller-behavior-multi-host.sh"

if [ -z "${GOALFLIGHT_LIVE_CONTROLLERS:-}" ]; then
  echo "SKIP  $SELF (set GOALFLIGHT_LIVE_CONTROLLERS=codex,claude-acp)"
  exit 0
fi

PYTHON_BIN="$(command -v python3 || true)"
if [ -z "$PYTHON_BIN" ]; then
  echo "SKIP  $SELF (python3 missing)"
  exit 0
fi

TRANSCRIPT_DIR="${GOALFLIGHT_CONTROLLER_TRANSCRIPT_DIR:-$REPO_ROOT/docs-private/reviews/$(date +%F)-chunk-16}"
SUMMARY_JSON="${GOALFLIGHT_CONTROLLER_SUMMARY_JSON:-$TRANSCRIPT_DIR/summary.json}"
PLAN_JSON="/tmp/controller-behavior-multi-host-plan-$$.json"
RESULT_DIR="/tmp/controller-behavior-multi-host-results-$$"
mkdir -p "$TRANSCRIPT_DIR" "$RESULT_DIR"

if ! "$PYTHON_BIN" - <<'PY' "$PROBE" "$RUNNER" "${GOALFLIGHT_LIVE_CONTROLLERS:-}" "$PLAN_JSON"
import importlib.util
import json
import sys
from pathlib import Path

runner_path = Path(sys.argv[2])
requested_text = sys.argv[3]
plan_path = Path(sys.argv[4])

def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module

runner = load_module("behavior_scenario_for_multi_host", runner_path)
plan = runner.build_multi_host_plan(requested_text)
plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
PY
then
  echo "FAIL  $SELF (could not build controller plan)"
  exit 1
fi

SELECTED_COUNT="$("$PYTHON_BIN" - <<'PY' "$PLAN_JSON"
import json, sys
payload = json.load(open(sys.argv[1]))
print(len(payload.get("selected_controllers") or []))
PY
)"
UNSUPPORTED_COUNT="$("$PYTHON_BIN" - <<'PY' "$PLAN_JSON"
import json, sys
payload = json.load(open(sys.argv[1]))
print(len(payload.get("available_unsupported_controllers") or []))
PY
)"
case "$SELECTED_COUNT" in
  ''|*[!0-9]*)
    echo "FAIL  $SELF (invalid selected controller count: $SELECTED_COUNT)"
    exit 1
    ;;
esac
case "$UNSUPPORTED_COUNT" in
  ''|*[!0-9]*)
    echo "FAIL  $SELF (invalid unsupported controller count: $UNSUPPORTED_COUNT)"
    exit 1
    ;;
esac

if [ "$SELECTED_COUNT" -eq 0 ]; then
  if [ "$UNSUPPORTED_COUNT" -gt 0 ]; then
    "$PYTHON_BIN" - <<'PY' "$PLAN_JSON"
import json, sys
payload = json.load(open(sys.argv[1]))
print(
    "FAIL  tests/bash/test-controller-behavior-multi-host.sh "
    f"(requested controllers are available but unsupported by behavior_scenario.py; "
    f"unsupported={payload.get('available_unsupported_controllers')}, "
    f"supported={payload.get('supported_controllers')})"
)
PY
    exit 1
  fi
  "$PYTHON_BIN" - <<'PY' "$PLAN_JSON"
import json, sys
payload = json.load(open(sys.argv[1]))
print(
    "SKIP  tests/bash/test-controller-behavior-multi-host.sh "
    f"(no available requested controllers; requested={payload.get('requested_controllers')}, "
    f"available={payload.get('available_controllers')})"
)
PY
  exit 0
fi

FAIL=0

while IFS= read -r HOST; do
  [ -n "$HOST" ] || continue
  while IFS= read -r SCENARIO; do
    [ -n "$SCENARIO" ] || continue
    JSON_OUT="$RESULT_DIR/$HOST-$SCENARIO.json"
    ERR_OUT="$RESULT_DIR/$HOST-$SCENARIO.err"
    RESULT_OUT="$RESULT_DIR/$HOST-$SCENARIO.summary.json"
    RAW_TRANSCRIPT="$TRANSCRIPT_DIR/$SCENARIO.transcript.log"
    TRANSCRIPT_OUT="$TRANSCRIPT_DIR/$HOST-$SCENARIO.transcript.log"
    rm -f "$RAW_TRANSCRIPT" "$TRANSCRIPT_OUT"

    if "$PYTHON_BIN" "$RUNNER" \
      --controller "$HOST" \
      --scenario "$SCENARIO" \
      --directory "$REPO_ROOT" \
      --transcript-dir "$TRANSCRIPT_DIR" \
      --json \
      > "$JSON_OUT" 2>"$ERR_OUT"; then
      RUN_RC=0
    else
      RUN_RC=$?
    fi

    if [ -f "$RAW_TRANSCRIPT" ]; then
      mv "$RAW_TRANSCRIPT" "$TRANSCRIPT_OUT"
    else
      {
        echo "controller: $HOST"
        echo "scenario: $SCENARIO"
        echo "runner_returncode: $RUN_RC"
        echo
        echo "STDOUT_JSON:"
        cat "$JSON_OUT"
        echo
        echo "STDERR:"
        cat "$ERR_OUT"
      } > "$TRANSCRIPT_OUT"
    fi

    if ! "$PYTHON_BIN" - <<'PY' "$JSON_OUT" "$ERR_OUT" "$HOST" "$SCENARIO" "$TRANSCRIPT_OUT" "$RUN_RC" "$RESULT_OUT"
import json
import sys
from pathlib import Path

json_path = Path(sys.argv[1])
err_path = Path(sys.argv[2])
host = sys.argv[3]
scenario = sys.argv[4]
transcript_path = Path(sys.argv[5])
returncode = int(sys.argv[6])
result_path = Path(sys.argv[7])

payload = {}
parse_error = None
try:
    if json_path.read_text(encoding="utf-8").strip():
        payload = json.loads(json_path.read_text(encoding="utf-8"))
except Exception as exc:  # noqa: BLE001
    parse_error = str(exc)

checks = payload.get("checks") if isinstance(payload, dict) else []
checks_ok = all(check.get("ok") for check in (checks or []))
skipped = bool(isinstance(payload, dict) and payload.get("skipped"))
ok = bool(isinstance(payload, dict) and payload.get("ok")) and checks_ok and returncode == 0
status = "SKIP" if skipped else ("PASS" if ok else "FAIL")

summary = {
    "controller": host,
    "scenario": scenario,
    "status": status.lower(),
    "ok": ok,
    "skipped": skipped,
    "returncode": returncode,
    "transcript_path": str(transcript_path),
    "json_path": str(json_path),
    "stderr_path": str(err_path),
    "elapsed_s": payload.get("elapsed_s") if isinstance(payload, dict) else None,
    "skip_reason": payload.get("skip_reason") if isinstance(payload, dict) else None,
    "check_ids": [check.get("id") for check in (checks or [])],
    "parse_error": parse_error,
}
result_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

print(f"{status}  {host}/{scenario} transcript={transcript_path}")
if status == "FAIL":
    stderr = err_path.read_text(encoding="utf-8").strip()
    if stderr:
        print("      stderr: " + stderr[:800].replace("\n", "\n      "))
sys.exit(1 if status == "FAIL" else 0)
PY
    then
      FAIL=1
    fi
  done < <("$PYTHON_BIN" - <<'PY' "$PLAN_JSON"
import json, sys
payload = json.load(open(sys.argv[1]))
print("\n".join(payload.get("scenarios") or []))
PY
)
done < <("$PYTHON_BIN" - <<'PY' "$PLAN_JSON"
import json, sys
payload = json.load(open(sys.argv[1]))
print("\n".join(payload.get("selected_controllers") or []))
PY
)

if ! "$PYTHON_BIN" - <<'PY' "$PLAN_JSON" "$RESULT_DIR" "$SUMMARY_JSON" "$TRANSCRIPT_DIR"
import datetime as dt
import json
import sys
from pathlib import Path

plan = json.load(open(sys.argv[1]))
result_dir = Path(sys.argv[2])
summary_path = Path(sys.argv[3])
transcript_dir = Path(sys.argv[4])
results = []
for path in sorted(result_dir.glob("*.summary.json")):
    results.append(json.loads(path.read_text(encoding="utf-8")))

totals = {
    "passed": sum(1 for item in results if item.get("status") == "pass"),
    "failed": sum(1 for item in results if item.get("status") == "fail"),
    "skipped": sum(1 for item in results if item.get("status") == "skip"),
}
summary = {
    "schema": "goalflight.controller-behavior.multi-host.v1",
    "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    "env_gate": "GOALFLIGHT_LIVE_CONTROLLERS",
    "requested_controllers": plan.get("requested_controllers") or [],
    "available_controllers": plan.get("available_controllers") or [],
    "supported_controllers": plan.get("supported_controllers") or [],
    "selected_controllers": plan.get("selected_controllers") or [],
    "unknown_controllers": plan.get("unknown_controllers") or [],
    "unavailable_controllers": plan.get("unavailable_controllers") or [],
    "available_unsupported_controllers": plan.get("available_unsupported_controllers") or [],
    "scenarios": plan.get("scenarios") or [],
    "transcript_dir": str(transcript_dir),
    "results": results,
    "totals": totals,
}
summary_path.parent.mkdir(parents=True, exist_ok=True)
summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
print(f"INFO  summary={summary_path}")
print(f"INFO  totals={totals}")
PY
then
  echo "FAIL  $SELF (could not write summary JSON)"
  FAIL=1
fi

if [ "$FAIL" -ne 0 ]; then
  echo "FAIL  $SELF"
  exit 1
fi

echo "PASS  $SELF"
