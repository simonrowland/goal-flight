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

# Append terminal marker to tail
echo "**COMPLETE:** test fixture done" >> "$TAIL"

# Wait for watcher to exit
wait "$WATCHER_PID"
watcher_exit=$?
expect_eq "case-1 exit code on terminal-marker" "0" "$watcher_exit"
if [ -f "$PIDFILE_DIR/$PIDFILE_STEM" ]; then
  expect_eq "case-1 pidfile removed on exit" "removed" "still-present"
else
  expect_eq "case-1 pidfile removed on exit" "removed" "removed"
fi
if grep -q "WATCHER-EXIT: marker exit_code=0" /tmp/watcher-out-marker-$$.txt; then
  expect_eq "case-1 WATCHER-EXIT summary line emitted" "yes" "yes"
else
  expect_eq "case-1 WATCHER-EXIT summary line emitted" "yes" "no"
fi
kill "$WORKER_PID" 2>/dev/null
wait "$WORKER_PID" 2>/dev/null
rm -f "$TAIL" /tmp/watcher-out-marker-$$.txt
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
kill "$WORKER_PID" 2>/dev/null
wait "$WORKER_PID" 2>/dev/null
rm -f "$TAIL" /tmp/watcher-out-controller-$$.txt
cleanup_pidfile "$PIDFILE_STEM"

# ---- Summary ----
echo "===== watch-dispatch-tail: $pass passed, $fail failed ====="
exit $fail
