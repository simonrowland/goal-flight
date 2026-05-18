#!/usr/bin/env bash
# Smoke + lifecycle test for scripts/watch-dispatch-tail.sh.
#
# Covers all four exit conditions:
#   - exit 0: terminal marker appears in tail (canonical happy path)
#   - exit 1: worker PID dies without any terminal marker
#   - exit 2: idle timeout (no tail update for max-idle-secs)
#   - exit 3: controller PID dies (orphan watcher self-detection)
#
# And pidfile lifecycle:
#   - on startup: per-watcher subfile written under /tmp/goal-flight-acp-pids.d/
#   - on clean exit (any code): subfile removed via EXIT trap

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WATCHER="$REPO_ROOT/scripts/watch-dispatch-tail.sh"
PIDFILE_DIR=/tmp/goal-flight-acp-pids.d

pass=0
fail=0
note() { echo "  $*" >&2; }
expect_eq() {
  local label="$1" expected="$2" got="$3"
  if [ "$got" = "$expected" ]; then
    note "PASS  $label  ($got)"
    pass=$((pass + 1))
  else
    echo "FAIL  $label  expected=$expected got=$got" >&2
    fail=$((fail + 1))
  fi
}
cleanup_pidfile() {
  local stem="$1"
  rm -f "$PIDFILE_DIR/$stem"
}

# Spawn workers directly in the test shell (NOT in a subshell via `$(spawn_fn)`
# — that kills the child when the subshell exits). Plain background, capture
# $! from the same shell scope.

# ---- Case 1: terminal marker → exit 0 ----
TAIL=/tmp/test-watch-marker-$$.txt
: > "$TAIL"
sleep 30 & WORKER_PID=$!
SLUG="test-marker"
PIDFILE_STEM="$$.bashtail.${WORKER_PID}.jsonl"

# Start watcher in background with aggressive poll for quick test cycle.
bash "$WATCHER" \
  --pid "$WORKER_PID" --tail "$TAIL" \
  --controller-pid "$$" --agent test-bashtail \
  --session-id "$SLUG" \
  --poll-secs 1 --max-idle-secs 30 \
  > /tmp/watcher-out-marker-$$.txt 2>&1 &
WATCHER_PID=$!
sleep 1

# Verify pidfile exists
if [ -f "$PIDFILE_DIR/$PIDFILE_STEM" ]; then
  expect_eq "case-1 pidfile written at startup" "yes" "yes"
else
  expect_eq "case-1 pidfile written at startup" "yes" "no"
fi

# Append terminal marker to tail. Worker is still alive (sleep 30 doesn't
# exit just because we wrote a marker to a different file). By the new exit-
# trap contract, the pidfile must be PRESERVED on exit because the worker is
# still alive — cleanup_ghosts() on a subsequent controller startup is what
# reaps the still-alive-but-orphaned worker.
echo "**COMPLETE:** test fixture done" >> "$TAIL"

# Wait for watcher to exit
wait "$WATCHER_PID"
watcher_exit=$?
expect_eq "case-1 exit code on terminal-marker" "0" "$watcher_exit"
# New behavior post-hardening: pidfile PRESERVED when worker still alive.
if [ -f "$PIDFILE_DIR/$PIDFILE_STEM" ]; then
  expect_eq "case-1 pidfile preserved (worker still alive on exit)" "preserved" "preserved"
else
  expect_eq "case-1 pidfile preserved (worker still alive on exit)" "preserved" "removed"
fi
if grep -q "WATCHER-EXIT: marker exit_code=0" /tmp/watcher-out-marker-$$.txt; then
  expect_eq "case-1 WATCHER-EXIT summary line emitted" "yes" "yes"
else
  expect_eq "case-1 WATCHER-EXIT summary line emitted" "yes" "no"
fi
# Kill worker + cleanup pidfile manually (test-controlled, not via watcher trap)
kill "$WORKER_PID" 2>/dev/null
wait "$WORKER_PID" 2>/dev/null
rm -f "$TAIL" /tmp/watcher-out-marker-$$.txt
cleanup_pidfile "$PIDFILE_STEM"

# ---- Case 1b: marker received + worker also dead → pidfile REMOVED ----
# Same as case 1 but the worker exits before the watcher exits, so the trap
# removes the pidfile.
TAIL=/tmp/test-watch-marker-dead-$$.txt
: > "$TAIL"
sleep 1 & WORKER_PID=$!
PIDFILE_STEM="$$.bashtail.${WORKER_PID}.jsonl"

bash "$WATCHER" \
  --pid "$WORKER_PID" --tail "$TAIL" \
  --controller-pid "$$" --agent test-bashtail \
  --session-id "test-marker-dead" \
  --poll-secs 1 --max-idle-secs 30 \
  > /tmp/watcher-out-marker-dead-$$.txt 2>&1 &
WATCHER_PID=$!
sleep 0.3
echo "**COMPLETE:** done" >> "$TAIL"
# Worker exits at +1s; watcher should see marker, exit 0, and remove pidfile
# (worker is gone by the time the trap runs).
wait "$WORKER_PID" 2>/dev/null
sleep 0.2  # let watcher tick once more so kill -0 returns false
wait "$WATCHER_PID"
watcher_exit=$?
expect_eq "case-1b exit code (marker + worker dead)" "0" "$watcher_exit"
if [ -f "$PIDFILE_DIR/$PIDFILE_STEM" ]; then
  expect_eq "case-1b pidfile removed (worker dead on exit)" "removed" "still-present"
else
  expect_eq "case-1b pidfile removed (worker dead on exit)" "removed" "removed"
fi
rm -f "$TAIL" /tmp/watcher-out-marker-dead-$$.txt
cleanup_pidfile "$PIDFILE_STEM"

# ---- Case 2: worker PID dies without marker → exit 1 ----
TAIL=/tmp/test-watch-piddead-$$.txt
: > "$TAIL"
sleep 2 & WORKER_PID=$!
PIDFILE_STEM="$$.bashtail.${WORKER_PID}.jsonl"

bash "$WATCHER" \
  --pid "$WORKER_PID" --tail "$TAIL" \
  --controller-pid "$$" --agent test-bashtail \
  --session-id "test-piddead" \
  --poll-secs 1 --max-idle-secs 30 \
  > /tmp/watcher-out-piddead-$$.txt 2>&1 &
WATCHER_PID=$!
# Worker exits naturally after 2s; watcher should detect and exit 1.
wait "$WATCHER_PID"
watcher_exit=$?
expect_eq "case-2 exit code on pid-dead-no-marker" "1" "$watcher_exit"
if grep -q "WATCHER-EXIT: pid-dead exit_code=1" /tmp/watcher-out-piddead-$$.txt; then
  expect_eq "case-2 WATCHER-EXIT summary line emitted" "yes" "yes"
else
  expect_eq "case-2 WATCHER-EXIT summary line emitted" "yes" "no"
fi
rm -f "$TAIL" /tmp/watcher-out-piddead-$$.txt
cleanup_pidfile "$PIDFILE_STEM"

# ---- Case 3: idle timeout → exit 2 ----
TAIL=/tmp/test-watch-idle-$$.txt
: > "$TAIL"
sleep 30 & WORKER_PID=$!
PIDFILE_STEM="$$.bashtail.${WORKER_PID}.jsonl"

# Aggressive: max-idle-secs=2 so test runs in seconds. Real defaults are 180s.
bash "$WATCHER" \
  --pid "$WORKER_PID" --tail "$TAIL" \
  --controller-pid "$$" --agent test-bashtail \
  --session-id "test-idle" \
  --poll-secs 1 --max-idle-secs 2 \
  > /tmp/watcher-out-idle-$$.txt 2>&1 &
WATCHER_PID=$!
wait "$WATCHER_PID"
watcher_exit=$?
expect_eq "case-3 exit code on idle-timeout" "2" "$watcher_exit"
if grep -q "WATCHER-EXIT: idle-timeout exit_code=2" /tmp/watcher-out-idle-$$.txt; then
  expect_eq "case-3 WATCHER-EXIT summary line emitted" "yes" "yes"
else
  expect_eq "case-3 WATCHER-EXIT summary line emitted" "yes" "no"
fi
# Idle exit: worker is still alive (just wedged). Pidfile must be PRESERVED
# so cleanup_ghosts() reaps the wedged worker on next controller startup.
if [ -f "$PIDFILE_DIR/$PIDFILE_STEM" ]; then
  expect_eq "case-3 pidfile preserved (worker still alive after idle timeout)" "preserved" "preserved"
else
  expect_eq "case-3 pidfile preserved (worker still alive after idle timeout)" "preserved" "removed"
fi
kill "$WORKER_PID" 2>/dev/null
wait "$WORKER_PID" 2>/dev/null
rm -f "$TAIL" /tmp/watcher-out-idle-$$.txt
cleanup_pidfile "$PIDFILE_STEM"

# ---- Case 4: controller PID dies → exit 3 ----
# Spawn a controller-proxy that exits in 2s. The watcher monitors it as its
# --controller-pid. After 2s the proxy dies; watcher should detect and exit 3.
TAIL=/tmp/test-watch-controller-$$.txt
: > "$TAIL"
sleep 30 & WORKER_PID=$!
sleep 2 & CONTROLLER_PROXY=$!
PIDFILE_STEM="${CONTROLLER_PROXY}.bashtail.${WORKER_PID}.jsonl"

bash "$WATCHER" \
  --pid "$WORKER_PID" --tail "$TAIL" \
  --controller-pid "$CONTROLLER_PROXY" --agent test-bashtail \
  --session-id "test-controller-dead" \
  --poll-secs 1 --max-idle-secs 30 \
  > /tmp/watcher-out-controller-$$.txt 2>&1 &
WATCHER_PID=$!
wait "$WATCHER_PID"
watcher_exit=$?
expect_eq "case-4 exit code on controller-dead" "3" "$watcher_exit"
if grep -q "WATCHER-EXIT: controller-dead exit_code=3" /tmp/watcher-out-controller-$$.txt; then
  expect_eq "case-4 WATCHER-EXIT summary line emitted" "yes" "yes"
else
  expect_eq "case-4 WATCHER-EXIT summary line emitted" "yes" "no"
fi
# Controller-dead exit: worker is still alive (controller just died, worker
# unaffected). Pidfile MUST be preserved so cleanup_ghosts() on the next
# controller startup walks it, identity-verifies the worker, and reaps it.
# This is the load-bearing orphan-defense path the codex hardening reviewer
# specifically flagged in pass 3 — removing the pidfile here orphans the
# worker beyond cleanup_ghosts' reach.
if [ -f "$PIDFILE_DIR/$PIDFILE_STEM" ]; then
  expect_eq "case-4 pidfile preserved (worker survives controller death)" "preserved" "preserved"
else
  expect_eq "case-4 pidfile preserved (worker survives controller death)" "preserved" "removed"
fi
kill "$WORKER_PID" 2>/dev/null
wait "$WORKER_PID" 2>/dev/null
rm -f "$TAIL" /tmp/watcher-out-controller-$$.txt
cleanup_pidfile "$PIDFILE_STEM"

# ---- Case 5: argument validation under macOS default bash 3.2 ----
# Runs the watcher with no args / bad args under /bin/bash explicitly to verify
# (a) exit code 64 (EX_USAGE) and (b) no "bad substitution" leak from bash-4-only
# parameter expansion patterns. The original 0.3.1 version had `${var,,}` on the
# missing-arg path which fails on bash 3.2 — this is the regression guard.
err=/tmp/test-watch-arg-validation-$$.err
/bin/bash "$WATCHER" > /dev/null 2> "$err"
expect_eq "case-5a exit code on no args" "64" "$?"
if grep -q "bad substitution" "$err"; then
  expect_eq "case-5a no bash-3.2 bad-substitution leak" "clean" "leaked"
else
  expect_eq "case-5a no bash-3.2 bad-substitution leak" "clean" "clean"
fi
rm -f "$err"

err=/tmp/test-watch-arg-validation-$$.err
/bin/bash "$WATCHER" --pid abc --tail /tmp/x --controller-pid 123 \
  --agent x --session-id x > /dev/null 2> "$err"
expect_eq "case-5b exit code on non-integer --pid" "64" "$?"
if grep -q "invalid --pid 'abc'" "$err"; then
  expect_eq "case-5b helpful error message" "yes" "yes"
else
  expect_eq "case-5b helpful error message" "yes" "no"
fi
rm -f "$err"

err=/tmp/test-watch-arg-validation-$$.err
/bin/bash "$WATCHER" --pid 123 --tail /tmp/x --controller-pid xyz \
  --agent x --session-id x > /dev/null 2> "$err"
expect_eq "case-5c exit code on non-integer --controller-pid" "64" "$?"
rm -f "$err"

# ---- Summary ----
echo "===== watch-dispatch-tail: $pass passed, $fail failed ====="
exit $fail
