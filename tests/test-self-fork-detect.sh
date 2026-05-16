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


# Test 7: find-fork with no new JSONLs returns empty.
if [ -n "${CLAUDE_CODE_SESSION_ID:-}" ]; then
  "$SCRIPT" write "test 7 task" ".fork-contract-7.json" >/dev/null
  result=$("$SCRIPT" find-fork ".fork-contract-7.json" 2>&1)
  if [ -z "$result" ]; then
    echo "test7 pass: find-fork on fresh snapshot returns empty"
  else
    # Could be non-empty if another fork exists from earlier in the session;
    # accept as long as it's a list of jsonl paths and doesn't error.
    if echo "$result" | head -1 | grep -q "\.jsonl$"; then
      echo "test7 pass: find-fork returned $(echo "$result" | wc -l | tr -d ' ') JSONLs (pre-existing forks from this session — acceptable)"
    else
      echo "test7 FAIL: unexpected find-fork output: $result"
      exit 1
    fi
  fi
fi

# Test 8: find-fork detects a synthetic new JSONL.
if [ -n "${CLAUDE_CODE_SESSION_ID:-}" ]; then
  PROJ_DIR="$HOME/.claude/projects"
  # Create a synthetic JSONL with a fake session ID that didn't exist at write time.
  FAKE_SID="ffff0000-test-test-test-${RANDOM}aaaa"
  # Find or create the project subdir matching our cwd shape
  SUBDIR=$(ls -d "$PROJ_DIR"/* 2>/dev/null | head -1)
  if [ -z "$SUBDIR" ]; then
    echo "test8 SKIP: no project dirs under ~/.claude/projects/"
  else
    FAKE_JSONL="$SUBDIR/${FAKE_SID}.jsonl"
    echo '{"type":"prompt","message":{"role":"user","content":[{"type":"text","text":"FORK-COMPLETE: done"}]}}' > "$FAKE_JSONL"
    result=$("$SCRIPT" find-fork ".fork-contract-7.json" 2>&1)
    if echo "$result" | grep -q "$FAKE_SID"; then
      echo "test8 pass: find-fork detects synthetic new JSONL"
    else
      echo "test8 FAIL: find-fork did not list $FAKE_JSONL"
      echo "  output: $result"
      rm -f "$FAKE_JSONL"
      exit 1
    fi
    # Test 9: monitor sees FORK-COMPLETE and exits 0.
    timeout 30 "$SCRIPT" monitor "$FAKE_JSONL" --poll 1 --idle-stop 5 > /tmp/monitor-test-out.txt 2>&1 &
    MONITOR_PID=$!
    sleep 2
    # Append a fork-complete event (already in initial line, but make sure monitor reads it)
    echo '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"FORK-COMPLETE: synthetic test done"}]}}' >> "$FAKE_JSONL"
    set +e
    wait "$MONITOR_PID" 2>/dev/null
    rc=$?
    set -e
    if [ "$rc" = "0" ] && grep -q "FORK-COMPLETE" /tmp/monitor-test-out.txt; then
      echo "test9 pass: monitor exits 0 on FORK-COMPLETE marker"
    else
      echo "test9 FAIL: monitor rc=$rc; output below:"
      cat /tmp/monitor-test-out.txt
      rm -f "$FAKE_JSONL" /tmp/monitor-test-out.txt
      exit 1
    fi
    rm -f "$FAKE_JSONL" /tmp/monitor-test-out.txt
  fi
fi


# Test 10: write embeds marker_vocabulary in contract.
if [ -n "${CLAUDE_CODE_SESSION_ID:-}" ]; then
  "$SCRIPT" write "test 10 task" ".fork-contract-10.json" >/dev/null
  if python3 -c "import json,sys; c=json.load(open('.fork-contract-10.json')); v=c['fork_contract'].get('marker_vocabulary',{}); sys.exit(0 if all(k in v for k in ['FORK-STATUS','FORK-RESULT','FORK-NEED','FORK-COMPLETE','FORK-BLOCKED']) else 1)"; then
    echo "test10 pass: write embeds all 5 markers in marker_vocabulary"
  else
    echo "test10 FAIL: marker_vocabulary missing required keys"
    exit 1
  fi
fi

# Test 11: monitor exits 1 on FORK-BLOCKED.
if [ -n "${CLAUDE_CODE_SESSION_ID:-}" ]; then
  PROJ_DIR="$HOME/.claude/projects"
  SUBDIR=$(ls -d "$PROJ_DIR"/* 2>/dev/null | head -1)
  if [ -n "$SUBDIR" ]; then
    FAKE_BLOCKED_SID="bbbb1111-test-test-test-${RANDOM}cccc"
    FAKE_BLOCKED_JSONL="$SUBDIR/${FAKE_BLOCKED_SID}.jsonl"
    echo '{"type":"prompt","message":{"role":"user","content":[{"type":"text","text":"start"}]}}' > "$FAKE_BLOCKED_JSONL"
    timeout 30 "$SCRIPT" monitor "$FAKE_BLOCKED_JSONL" --poll 1 --idle-stop 5 > /tmp/monitor-blocked-out.txt 2>&1 &
    MPID=$!
    sleep 2
    echo '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"FORK-BLOCKED: synthetic missing-dep test"}]}}' >> "$FAKE_BLOCKED_JSONL"
    set +e
    wait "$MPID" 2>/dev/null
    rc=$?
    set -e
    if [ "$rc" = "1" ] && grep -q "FORK-BLOCKED" /tmp/monitor-blocked-out.txt; then
      echo "test11 pass: monitor exits 1 on FORK-BLOCKED"
    else
      echo "test11 FAIL: expected rc=1; got rc=$rc"
      cat /tmp/monitor-blocked-out.txt
      rm -f "$FAKE_BLOCKED_JSONL" /tmp/monitor-blocked-out.txt
      exit 1
    fi
    rm -f "$FAKE_BLOCKED_JSONL" /tmp/monitor-blocked-out.txt
  fi
fi

# Test 12: monitor exits 2 on FORK-NEED (intervention required).
if [ -n "${CLAUDE_CODE_SESSION_ID:-}" ]; then
  PROJ_DIR="$HOME/.claude/projects"
  SUBDIR=$(ls -d "$PROJ_DIR"/* 2>/dev/null | head -1)
  if [ -n "$SUBDIR" ]; then
    FAKE_NEED_SID="cccc1111-test-test-test-${RANDOM}dddd"
    FAKE_NEED_JSONL="$SUBDIR/${FAKE_NEED_SID}.jsonl"
    echo '{"type":"prompt","message":{"role":"user","content":[{"type":"text","text":"start"}]}}' > "$FAKE_NEED_JSONL"
    timeout 30 "$SCRIPT" monitor "$FAKE_NEED_JSONL" --poll 1 --idle-stop 5 > /tmp/monitor-need-out.txt 2>&1 &
    MPID=$!
    sleep 2
    echo '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"FORK-NEED: confirm if X is in-scope"}]}}' >> "$FAKE_NEED_JSONL"
    set +e
    wait "$MPID" 2>/dev/null
    rc=$?
    set -e
    if [ "$rc" = "2" ] && grep -q "FORK-NEED" /tmp/monitor-need-out.txt; then
      echo "test12 pass: monitor exits 2 on FORK-NEED"
    else
      echo "test12 FAIL: expected rc=2; got rc=$rc"
      cat /tmp/monitor-need-out.txt
      rm -f "$FAKE_NEED_JSONL" /tmp/monitor-need-out.txt
      exit 1
    fi
    rm -f "$FAKE_NEED_JSONL" /tmp/monitor-need-out.txt
  fi
fi


# Test 13: reply mode rejects missing args.
set +e
out=$("$SCRIPT" reply 2>&1)
rc=$?
set -e
if [ "$rc" != "0" ] && echo "$out" | grep -q "usage:"; then
  echo "test13 pass: reply mode rejects missing args (exit $rc)"
else
  echo "test13 FAIL: reply with no args should error; got rc=$rc, out=$out"
  exit 1
fi

# Test 14: reply mode rejects malformed UUID.
set +e
out=$("$SCRIPT" reply "not-a-uuid" "test reply" 2>&1)
rc=$?
set -e
if [ "$rc" != "0" ] && echo "$out" | grep -q "doesn't match UUID shape"; then
  echo "test14 pass: reply mode rejects malformed UUID (exit $rc)"
else
  echo "test14 FAIL: reply with bad UUID should error; got rc=$rc"
  exit 1
fi

echo
echo "all self-fork-detect tests passed"
