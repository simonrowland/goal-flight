#!/usr/bin/env bash
# Watcher marker contract for the Moonshot (kimi CLI) lane.
# Case 1: the current handle (--agent moonshot) accepts the kimi bullet-style
#         terminal marker.
# Case 2 (legacy regression): a watcher pointed at a LEGACY record carrying the
#         retired agent value (--agent kimi) must accept the same marker —
#         history keeps reconciling exactly as before the rename.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/goalflight-moonshot-watch-test-XXXXXX")"
WORKER_PID=""
cleanup() {
  if [ -n "$WORKER_PID" ]; then
    kill "$WORKER_PID" 2>/dev/null || true
    wait "$WORKER_PID" 2>/dev/null || true
  fi
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

run_watch_case() {
  local agent_label="$1"
  local tail_file="$TMP_ROOT/$agent_label.tail"
  local watcher_out="$TMP_ROOT/watcher-$agent_label.out"
  printf '• COMPLETE: true\n' > "$tail_file"

  sleep 3 &
  WORKER_PID=$!
  GOAL_FLIGHT_PIDFILE_DIR="$TMP_ROOT/pids" \
    bash "$REPO_ROOT/scripts/watch-dispatch-tail.sh" \
      --pid "$WORKER_PID" \
      --tail "$tail_file" \
      --controller-pid "$$" \
      --agent "$agent_label" \
      --session-id "moonshot-marker-test-$agent_label" \
      --poll-secs 1 \
      --max-idle-secs 10 \
      > "$watcher_out" 2>&1
  local status=$?
  wait "$WORKER_PID" 2>/dev/null || true
  WORKER_PID=""

  if [ "$status" -ne 0 ] || ! grep -Fq 'WATCHER-EXIT: marker exit_code=0' "$watcher_out"; then
    echo "FAIL: watcher (--agent $agent_label) did not accept bullet terminal marker (exit=$status)" >&2
    sed 's/^/  /' "$watcher_out" >&2
    exit 1
  fi
  echo "OK: watcher (--agent $agent_label) recognizes bullet terminal marker"
}

mkdir -p "$TMP_ROOT/pids"
run_watch_case moonshot
run_watch_case kimi
