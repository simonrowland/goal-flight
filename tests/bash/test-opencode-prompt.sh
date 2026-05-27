#!/usr/bin/env bash
# Optional live smoke test for scripts/hosts/opencode/prompt.py.
# Skips when opencode or LiteLLM credentials are unavailable.

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/hosts/opencode/prompt.py"

if ! command -v opencode >/dev/null 2>&1; then
  echo "SKIP  tests/bash/test-opencode-prompt.sh (opencode not installed)"
  exit 0
fi

if [ -f "$HOME/.config/rpp/litellm.env" ]; then
  # shellcheck disable=SC1090
  source "$HOME/.config/rpp/litellm.env"
fi

if [ -z "${LITELLM_API_KEY:-}" ] && [ -z "${LITELLM_MASTER_KEY:-}" ]; then
  echo "SKIP  tests/bash/test-opencode-prompt.sh (LiteLLM env missing)"
  exit 0
fi

pkill -f 'opencode serve' >/dev/null 2>&1 || true
sleep 1

out="$("$SCRIPT" -m litellm/nano "Reply with exactly one word: pong" 2>&1)" || {
  echo "FAIL  tests/bash/test-opencode-prompt.sh"
  echo "$out" | sed 's/^/      /'
  exit 1
}

if ! printf '%s\n' "$out" | grep -qi '^pong$'; then
  echo "FAIL  tests/bash/test-opencode-prompt.sh (unexpected reply)"
  printf '%s\n' "$out" | sed 's/^/      /'
  exit 1
fi

echo "PASS  tests/bash/test-opencode-prompt.sh"
