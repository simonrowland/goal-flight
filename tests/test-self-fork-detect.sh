#!/usr/bin/env bash
# Tests for scripts/self-fork-detect.sh.
#
# Verifies the marker-roundtrip in the SAME session (since we can't trigger
# /fork from within a script). The fork case is exercised by the user
# running /fork interactively and re-running `self-fork-detect.sh detect`
# in the forked session — there's no scripted way to verify that without
# the user (or a spawned `claude --resume --fork-session` probe).

set -eu

SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/scripts/self-fork-detect.sh"
[ -x "$SCRIPT" ] || { echo "script not executable: $SCRIPT"; exit 1; }

TMPROOT=$(mktemp -d /tmp/goal-flight-fork-detect-test.XXXX)
trap 'rm -rf "$TMPROOT"' EXIT
cd "$TMPROOT"

# Test 1: detect with no contract → NO_CONTRACT.
result=$("$SCRIPT" detect ".fork-contract-1.json" 2>&1 | head -1)
[ "$result" = "NO_CONTRACT" ] \
  || { echo "test1 FAIL: expected NO_CONTRACT, got '$result'"; exit 1; }
echo "test1 pass: detect with no contract returns NO_CONTRACT"

# Test 2: write requires CLAUDE_CODE_SESSION_ID env var.
# We're running inside Claude Code so the env var should be set.
if [ -z "${CLAUDE_CODE_SESSION_ID:-}" ]; then
  echo "test2 SKIP: not running inside Claude Code; CLAUDE_CODE_SESSION_ID unset"
else
  "$SCRIPT" write "test task: verify marker roundtrip" ".fork-contract-2.json" >/dev/null
  [ -f ".fork-contract-2.json" ] \
    || { echo "test2 FAIL: contract file not written"; exit 1; }
  grep -q "controller_session_id" ".fork-contract-2.json" \
    || { echo "test2 FAIL: contract missing controller_session_id"; exit 1; }
  echo "test2 pass: write creates contract with controller_session_id"
fi

# Test 3: detect immediately after write in same session → ORIGINAL.
# This is the "no fork happened" case.
if [ -n "${CLAUDE_CODE_SESSION_ID:-}" ]; then
  result=$("$SCRIPT" detect ".fork-contract-2.json" 2>&1 | head -1)
  # Could be ORIGINAL or SUBAGENT depending on whether a subagent is mid-flight
  # right now (the heuristic looks at recent subagents/ activity). Both are
  # "not a fork" — accept either as success for this test.
  case "$result" in
    ORIGINAL|SUBAGENT)
      echo "test3 pass: detect after write returns $result (not FORK; same session)"
      ;;
    *)
      echo "test3 FAIL: expected ORIGINAL or SUBAGENT, got '$result'"
      exit 1
      ;;
  esac
fi

# Test 4: write with a different session_id in the contract → detect returns FORK.
# Simulates the post-fork state by manually editing the contract.
if [ -n "${CLAUDE_CODE_SESSION_ID:-}" ]; then
  cat > ".fork-contract-4.json" <<EOF
{
  "controller_session_id": "00000000-0000-0000-0000-000000000000",
  "marker_written_at": "2026-05-15T00:00:00Z",
  "fork_contract": {
    "task": "synthetic fork detection test",
    "completion_signal": "n/a",
    "abort_signal": "n/a"
  }
}
EOF
  result=$("$SCRIPT" detect ".fork-contract-4.json" 2>&1 | head -1)
  [ "$result" = "FORK" ] \
    || { echo "test4 FAIL: expected FORK (env=$CLAUDE_CODE_SESSION_ID, marker=0000...), got '$result'"; exit 1; }
  echo "test4 pass: synthetic mismatched marker triggers FORK"

  # Also check that the task line is printed.
  full_output=$("$SCRIPT" detect ".fork-contract-4.json" 2>&1)
  echo "$full_output" | grep -q "task: synthetic fork detection test" \
    || { echo "test4b FAIL: task line not printed on FORK"; exit 1; }
  echo "test4b pass: FORK output includes task line"
fi

# Test 5: clear removes the contract.
"$SCRIPT" clear ".fork-contract-2.json" >/dev/null 2>&1 || true
[ ! -f ".fork-contract-2.json" ] \
  || { echo "test5 FAIL: clear did not remove contract"; exit 1; }
echo "test5 pass: clear removes the contract"

# Test 6: clear on non-existent contract is harmless.
"$SCRIPT" clear ".no-such-contract.json" >/dev/null 2>&1
echo "test6 pass: clear on nonexistent contract exits cleanly"

echo
echo "all self-fork-detect tests passed (5-6 depending on whether \$CLAUDE_CODE_SESSION_ID was set)"
