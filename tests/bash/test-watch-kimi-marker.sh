#!/usr/bin/env bash
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/goalflight-kimi-watch-test-XXXXXX")"
WORKER_PID=""
cleanup() {
  if [ -n "$WORKER_PID" ]; then
    kill "$WORKER_PID" 2>/dev/null || true
    wait "$WORKER_PID" 2>/dev/null || true
  fi
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

TAIL_FILE="$TMP_ROOT/kimi.tail"
WATCHER_OUT="$TMP_ROOT/watcher.out"
PIDFILE_DIR="$TMP_ROOT/pids"
mkdir -p "$PIDFILE_DIR"
printf '• COMPLETE: true\n' > "$TAIL_FILE"

sleep 3 &
WORKER_PID=$!
GOAL_FLIGHT_PIDFILE_DIR="$PIDFILE_DIR" \
  bash "$REPO_ROOT/scripts/watch-dispatch-tail.sh" \
    --pid "$WORKER_PID" \
    --tail "$TAIL_FILE" \
    --controller-pid "$$" \
    --agent kimi \
    --session-id kimi-marker-test \
    --poll-secs 1 \
    --max-idle-secs 10 \
    > "$WATCHER_OUT" 2>&1
status=$?

if [ "$status" -ne 0 ] || ! grep -Fq 'WATCHER-EXIT: marker exit_code=0' "$WATCHER_OUT"; then
  echo "FAIL: canonical Kimi watcher did not accept bullet terminal marker (exit=$status)" >&2
  sed 's/^/  /' "$WATCHER_OUT" >&2
  exit 1
fi

echo "OK: canonical Kimi watcher recognizes bullet terminal marker"
