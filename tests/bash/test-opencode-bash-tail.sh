#!/usr/bin/env bash
# Optional live smoke test for opencode bash-tail worker + watcher integration.
# Skips when opencode or LiteLLM credentials are unavailable.

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORKER="$REPO_ROOT/scripts/hosts/opencode/bash_tail.py"
WATCHER="$REPO_ROOT/scripts/watch-dispatch-tail.sh"

# shellcheck source=tests/bash/lib-opencode-backend.sh
source "$REPO_ROOT/tests/bash/lib-opencode-backend.sh"

if ! command -v opencode >/dev/null 2>&1; then
  echo "SKIP  tests/bash/test-opencode-bash-tail.sh (opencode not installed)"
  exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "SKIP  tests/bash/test-opencode-bash-tail.sh (python3 missing)"
  exit 0
fi

if [ -f "$HOME/.config/rpp/litellm.env" ]; then
  # shellcheck disable=SC1090
  source "$HOME/.config/rpp/litellm.env"
fi

if [ -z "${LITELLM_API_KEY:-}" ] && [ -z "${LITELLM_MASTER_KEY:-}" ]; then
  echo "SKIP  tests/bash/test-opencode-bash-tail.sh (LiteLLM env missing)"
  exit 0
fi

pkill -f 'opencode serve' >/dev/null 2>&1 || true
sleep 1

WORKDIR="$(mktemp -d /tmp/opencode-bash-tail-work-XXXXXX)"
TAIL="$(mktemp /tmp/opencode-bash-tail-XXXXXX.txt)"
PROMPT="$(mktemp /tmp/opencode-bash-tail-prompt-XXXXXX.md)"
PIDFILE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/goal-flight-opencode-bash-tail-pids.XXXXXX")"
export GOALFLIGHT_PIDFILE_DIR="$PIDFILE_DIR"
cleanup() {
  rm -rf "$WORKDIR" "$PIDFILE_DIR"
  rm -f "$TAIL" "$PROMPT"
}
trap cleanup EXIT

cp "$REPO_ROOT/configs/opencode/opencode.json" "$WORKDIR/opencode.json"
cat > "$PROMPT" <<'EOF'
What is 2+2? Reply with just the number on one line.
EOF

python3 "$WORKER" \
  --directory "$WORKDIR" \
  --tail "$TAIL" \
  --prompt-file "$PROMPT" \
  --model litellm/nano \
  --timeout 120 \
  > /tmp/opencode-bash-tail-worker-meta-$$.txt 2>&1 &
WORKER_PID=$!

bash "$WATCHER" \
  --pid "$WORKER_PID" \
  --tail "$TAIL" \
  --ignore-prompt-file "$PROMPT" \
  --controller-pid "$$" \
  --agent opencode-bash-tail \
  --session-id opencode-bash-tail-smoke \
  --poll-secs 1 \
  --max-idle-secs 120 \
  > /tmp/opencode-bash-tail-watcher-$$.txt 2>&1 &
WATCHER_PID=$!

if ! wait "$WORKER_PID"; then
  BACKEND_LOG="/tmp/opencode-bash-tail-backend-$$.txt"
  cat /tmp/opencode-bash-tail-worker-meta-$$.txt "$TAIL" > "$BACKEND_LOG"
  if opencode_backend_unhealthy_log "$BACKEND_LOG"; then
    opencode_backend_skip "tests/bash/test-opencode-bash-tail.sh"
  fi
  echo "FAIL  tests/bash/test-opencode-bash-tail.sh (worker exited non-zero)"
  cat /tmp/opencode-bash-tail-worker-meta-$$.txt | sed 's/^/      /'
  cat "$TAIL" | sed 's/^/      /'
  exit 1
fi

if ! wait "$WATCHER_PID"; then
  echo "FAIL  tests/bash/test-opencode-bash-tail.sh (watcher exited non-zero)"
  cat /tmp/opencode-bash-tail-watcher-$$.txt | sed 's/^/      /'
  cat "$TAIL" | sed 's/^/      /'
  exit 1
fi

if ! grep -q '^COMPLETE: true$' "$TAIL"; then
  echo "FAIL  tests/bash/test-opencode-bash-tail.sh (missing COMPLETE marker)"
  cat "$TAIL" | sed 's/^/      /'
  exit 1
fi

if ! grep -Eq '^[^C].*4|^4$' "$TAIL"; then
  echo "FAIL  tests/bash/test-opencode-bash-tail.sh (expected reply 4 in tail)"
  cat "$TAIL" | sed 's/^/      /'
  exit 1
fi

echo "PASS  tests/bash/test-opencode-bash-tail.sh"
