#!/usr/bin/env bash
# Optional live smoke test for Kimi bare print mode + watcher.
# Skips when Kimi is missing, unauthenticated, or the timeout helper is absent.

set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WATCHER="$REPO_ROOT/scripts/watch-dispatch-tail.sh"

KIMI="$(command -v kimi 2>/dev/null || true)"
if [ -z "$KIMI" ] && [ -x "$HOME/.kimi-code/bin/kimi" ]; then
  KIMI="$HOME/.kimi-code/bin/kimi"
fi

if [ -z "$KIMI" ]; then
  echo "SKIP  tests/test-kimi-bash-tail.sh (kimi not installed)"
  exit 0
fi

if ! command -v timeout >/dev/null 2>&1; then
  echo "SKIP  tests/test-kimi-bash-tail.sh (timeout command missing)"
  exit 0
fi

WORKDIR="$(mktemp -d /tmp/kimi-bash-tail-work-XXXXXX)"
TAIL="$(mktemp /tmp/kimi-bash-tail-XXXXXX.txt)"
PIDFILE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/goal-flight-kimi-bash-tail-pids.XXXXXX")"
WATCHER_OUT="$(mktemp /tmp/kimi-bash-tail-watcher-XXXXXX.txt)"
export GOAL_FLIGHT_PIDFILE_DIR="$PIDFILE_DIR"

TARGET="$WORKDIR/kimi-bash-tail-smoke.txt"
PROMPT="Create file kimi-bash-tail-smoke.txt with exactly the text done. Then output COMPLETE: true on its own line."

cleanup() {
  rm -rf "$WORKDIR" "$PIDFILE_DIR"
  rm -f "$TAIL" "$WATCHER_OUT"
}
trap cleanup EXIT

auth_skip_if_needed() {
  local log="$1"
  if grep -qiE 'auth|login|unauthorized|sign[ -]?in|authentication|not logged' "$log"; then
    echo "SKIP  tests/test-kimi-bash-tail.sh (kimi not authenticated)"
    exit 0
  fi
}

(
  cd "$WORKDIR" || exit 1
  exec timeout 120 "$KIMI" -p "$PROMPT" --output-format text
) > "$TAIL" 2>&1 &
WORKER_PID=$!

bash "$WATCHER" \
  --pid "$WORKER_PID" \
  --tail "$TAIL" \
  --controller-pid "$$" \
  --agent kimi-bash-tail \
  --session-id kimi-bash-tail-smoke \
  --poll-secs 1 \
  --max-idle-secs 120 \
  > "$WATCHER_OUT" 2>&1 &
WATCHER_PID=$!

WORKER_RC=0
WATCHER_RC=0
wait "$WORKER_PID" || WORKER_RC=$?
wait "$WATCHER_PID" || WATCHER_RC=$?

if [ "$WORKER_RC" -ne 0 ] || [ "$WATCHER_RC" -ne 0 ]; then
  auth_skip_if_needed "$TAIL"
  auth_skip_if_needed "$WATCHER_OUT"
  echo "FAIL  tests/test-kimi-bash-tail.sh (worker=$WORKER_RC watcher=$WATCHER_RC)"
  sed 's/^/      /' "$TAIL" || true
  sed 's/^/      /' "$WATCHER_OUT" || true
  exit 1
fi

if ! grep -Eq '^(•[[:space:]]*)?[[:space:]]*COMPLETE: true$' "$TAIL"; then
  auth_skip_if_needed "$TAIL"
  echo "FAIL  tests/test-kimi-bash-tail.sh (missing COMPLETE marker)"
  sed 's/^/      /' "$TAIL" || true
  exit 1
fi

if [ ! -f "$TARGET" ] || [ "$(tr -d '\n' < "$TARGET")" != "done" ]; then
  echo "FAIL  tests/test-kimi-bash-tail.sh (expected file with content 'done')"
  ls -la "$WORKDIR" | sed 's/^/      /' || true
  sed 's/^/      /' "$TAIL" || true
  exit 1
fi

echo "PASS  tests/test-kimi-bash-tail.sh"
