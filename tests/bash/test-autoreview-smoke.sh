#!/usr/bin/env bash
# Optional maintainer smoke test for the autoreview integration.

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TEST_NAME="tests/bash/test-autoreview-smoke.sh"
FIXTURE_COMMIT="${GOALFLIGHT_AUTOREVIEW_FIXTURE:-019794f}"
TMP_ROOT="${TMPDIR:-/tmp}/goal-flight-autoreview-smoke-$$"
OUTPUT_PATH="$TMP_ROOT/autoreview.txt"
trap 'rm -rf "$TMP_ROOT"' EXIT

if [ "${GOALFLIGHT_AUTOREVIEW:-}" != "1" ]; then
  echo "SKIP autoreview maintainer tier (set GOALFLIGHT_AUTOREVIEW=1)"
  exit 0
fi

mkdir -p "$TMP_ROOT"

if [ ! -x "$REPO_ROOT/scripts/autoreview.sh" ]; then
  echo "FAIL  $TEST_NAME"
  echo "      missing executable: scripts/autoreview.sh"
  exit 1
fi

if [ ! -f "$REPO_ROOT/scripts/autoreview_claude_acp" ]; then
  echo "FAIL  $TEST_NAME"
  echo "      missing ACP shim: scripts/autoreview_claude_acp"
  exit 1
fi

if ! grep -q 'autoreview_claude_acp' "$REPO_ROOT/scripts/autoreview.sh"; then
  echo "FAIL  $TEST_NAME"
  echo "      scripts/autoreview.sh does not reference scripts/autoreview_claude_acp"
  exit 1
fi

# --- grok-acp engine: static + hermetic guard coverage (no live grok runtime) ---

if [ ! -f "$REPO_ROOT/scripts/autoreview_grok_acp" ]; then
  echo "FAIL  $TEST_NAME"
  echo "      missing ACP shim: scripts/autoreview_grok_acp"
  exit 1
fi

if ! grep -q 'autoreview_grok_acp' "$REPO_ROOT/scripts/autoreview.sh"; then
  echo "FAIL  $TEST_NAME"
  echo "      scripts/autoreview.sh does not reference scripts/autoreview_grok_acp"
  exit 1
fi

# The grok-acp engine must refuse the raw grok CLI (which cannot do the ACP
# file-handoff) with a teaching error, instead of failing confusingly downstream.
grok_guard_out="$("$REPO_ROOT/autoreview/scripts/autoreview" --engine grok-acp --grok-bin grok --mode uncommitted 2>&1)"
if ! printf '%s' "$grok_guard_out" | grep -q 'requires the autoreview_grok_acp ACP shim'; then
  echo "FAIL  $TEST_NAME"
  echo "      grok-acp engine did not reject the raw grok CLI with the shim-required guard"
  exit 1
fi

# The ACP file-handoff requires write tools, so --no-tools must be rejected
# (matches codex/copilot). Pass the real shim so the guard above is satisfied first.
grok_notools_out="$("$REPO_ROOT/autoreview/scripts/autoreview" --engine grok-acp --grok-bin "$REPO_ROOT/scripts/autoreview_grok_acp" --no-tools --mode uncommitted 2>&1)"
if ! printf '%s' "$grok_notools_out" | grep -q 'no-tools is not supported by the grok-acp engine'; then
  echo "FAIL  $TEST_NAME"
  echo "      grok-acp engine did not reject --no-tools"
  exit 1
fi

if grep -Eq 'claude[[:space:]]+(-p|--print)' "$REPO_ROOT/scripts/autoreview.sh"; then
  echo "FAIL  $TEST_NAME"
  echo "      scripts/autoreview.sh must route Claude through ACP shim, not native print mode"
  exit 1
fi

"$REPO_ROOT/scripts/autoreview.sh" \
  --mode commit \
  --commit "$FIXTURE_COMMIT" \
  --engine claude \
  --prompt "Maintainer smoke test: this known-good fixture is expected to be clean. Report only concrete correctness or security regressions introduced by this commit." \
  --output "$OUTPUT_PATH" \
  > "$TMP_ROOT/stdout.txt" 2> "$TMP_ROOT/stderr.txt"
status=$?

if [ "$status" -ne 0 ]; then
  echo "FAIL  $TEST_NAME"
  echo "      autoreview exited $status"
  sed 's/^/      /' "$TMP_ROOT/stderr.txt"
  sed 's/^/      /' "$TMP_ROOT/stdout.txt"
  exit 1
fi

if [ ! -s "$OUTPUT_PATH" ]; then
  echo "FAIL  $TEST_NAME"
  echo "      expected output file missing or empty: $OUTPUT_PATH"
  exit 1
fi

if ! grep -Eq '^\[(P0|P1|P2|P3)\]|no accepted/actionable findings|no findings|overall:' "$OUTPUT_PATH"; then
  echo "FAIL  $TEST_NAME"
  echo "      output missing autoreview severity or clean marker: $OUTPUT_PATH"
  sed 's/^/      /' "$OUTPUT_PATH"
  exit 1
fi

echo "PASS  $TEST_NAME"
