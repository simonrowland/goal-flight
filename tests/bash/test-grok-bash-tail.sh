#!/usr/bin/env bash
# Optional live smoke test for grok bash-tail (--permission-mode auto) + watcher.
# Skips when grok is missing or not authenticated.

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WATCHER="$REPO_ROOT/scripts/watch-dispatch-tail.sh"

GROK="$(command -v grok 2>/dev/null || true)"
if [ -z "$GROK" ] && [ -x "$HOME/.grok/bin/grok" ]; then
  GROK="$HOME/.grok/bin/grok"
fi

if [ -z "$GROK" ]; then
  echo "SKIP  tests/bash/test-grok-bash-tail.sh (grok not installed)"
  exit 0
fi

WORKDIR="$(mktemp -d /tmp/grok-bash-tail-work-XXXXXX)"
TAIL="$(mktemp /tmp/grok-bash-tail-XXXXXX.txt)"
PIDFILE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/goal-flight-grok-bash-tail-pids.XXXXXX")"
WORKER_META="$(mktemp /tmp/grok-bash-tail-worker-meta-XXXXXX.txt)"
WATCHER_OUT="$(mktemp /tmp/grok-bash-tail-watcher-XXXXXX.txt)"
export GOAL_FLIGHT_PIDFILE_DIR="$PIDFILE_DIR"

TARGET="$WORKDIR/grok-bash-tail-smoke.txt"
PROMPT="Create file grok-bash-tail-smoke.txt with exactly the text done. Then output COMPLETE: true on its own line."

cleanup() {
  rm -rf "$WORKDIR" "$PIDFILE_DIR"
  rm -f "$TAIL" "$WORKER_META" "$WATCHER_OUT"
}
trap cleanup EXIT

auth_skip_if_needed() {
  local log="$1"
  if grep -qiE 'auth|login|api.?key|unauthorized|sign[ -]?in|authentication|not logged' "$log"; then
    echo "SKIP  tests/bash/test-grok-bash-tail.sh (grok not authenticated)"
    exit 0
  fi
}

if ! command -v timeout >/dev/null 2>&1; then
  echo "SKIP  tests/bash/test-grok-bash-tail.sh (timeout command missing)"
  exit 0
fi

timeout 120 "$GROK" -p "$PROMPT" \
  --permission-mode auto \
  --cwd "$WORKDIR" \
  --output-format plain \
  > "$TAIL" 2>&1 &
WORKER_PID=$!

bash "$WATCHER" \
  --pid "$WORKER_PID" \
  --tail "$TAIL" \
  --controller-pid "$$" \
  --agent grok-bash-tail \
  --session-id grok-bash-tail-smoke \
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
  echo "FAIL  tests/bash/test-grok-bash-tail.sh (worker=$WORKER_RC watcher=$WATCHER_RC)"
  sed 's/^/      /' "$TAIL" || true
  sed 's/^/      /' "$WATCHER_OUT" || true
  exit 1
fi

if ! grep -q '^COMPLETE: true$' "$TAIL"; then
  auth_skip_if_needed "$TAIL"
  echo "FAIL  tests/bash/test-grok-bash-tail.sh (missing COMPLETE marker)"
  sed 's/^/      /' "$TAIL" || true
  exit 1
fi

if [ ! -f "$TARGET" ] || [ "$(tr -d '\n' < "$TARGET")" != "done" ]; then
  echo "FAIL  tests/bash/test-grok-bash-tail.sh (expected file with content 'done')"
  ls -la "$WORKDIR" | sed 's/^/      /' || true
  sed 's/^/      /' "$TAIL" || true
  exit 1
fi

echo "PASS  tests/bash/test-grok-bash-tail.sh"
