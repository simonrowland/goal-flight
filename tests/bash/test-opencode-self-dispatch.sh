#!/usr/bin/env bash
# Live smoke test: OpenCode controller self-dispatch (ACP + bash-tail).
# Skips when opencode or LiteLLM credentials are unavailable.

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/hosts/opencode/self_dispatch_test.py"

if ! command -v opencode >/dev/null 2>&1; then
  echo "SKIP  tests/bash/test-opencode-self-dispatch.sh (opencode not installed)"
  exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "SKIP  tests/bash/test-opencode-self-dispatch.sh (python3 missing)"
  exit 0
fi

if [ -f "$HOME/.config/rpp/litellm.env" ]; then
  # shellcheck disable=SC1090
  source "$HOME/.config/rpp/litellm.env"
fi

if [ -z "${LITELLM_API_KEY:-}" ] && [ -z "${LITELLM_MASTER_KEY:-}" ]; then
  echo "SKIP  tests/bash/test-opencode-self-dispatch.sh (LiteLLM env missing)"
  exit 0
fi

pkill -f 'opencode serve' >/dev/null 2>&1 || true
sleep 1

if ! python3 "$SCRIPT" --directory "$REPO_ROOT" --json > /tmp/opencode-self-dispatch-$$.json 2>/tmp/opencode-self-dispatch-$$.err; then
  echo "FAIL  tests/bash/test-opencode-self-dispatch.sh"
  cat /tmp/opencode-self-dispatch-$$.err | sed 's/^/      /'
  cat /tmp/opencode-self-dispatch-$$.json | sed 's/^/      /'
  exit 1
fi

python3 - <<'PY' /tmp/opencode-self-dispatch-$$.json
import json, sys
payload = json.load(open(sys.argv[1]))
assert payload.get("ok"), payload
assert payload["transports"]["acp"]["ok"], payload["transports"]["acp"]
assert payload["transports"]["bash_tail"]["ok"], payload["transports"]["bash_tail"]
print("PASS  tests/bash/test-opencode-self-dispatch.sh")
PY
