#!/usr/bin/env bash
# Optional live smoke test for opencode acp as a Goal Flight worker.
# Skips when opencode, ACP venv, or LiteLLM credentials are unavailable.

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ACP_PY="${GOALFLIGHT_ACP_PYTHON:-$HOME/.goal-flight/venvs/acp-0.10/bin/python}"
PROBE="$REPO_ROOT/tests/python/probe_real_worker.py"

if ! command -v opencode >/dev/null 2>&1; then
  echo "SKIP  tests/bash/test-opencode-acp.sh (opencode not installed)"
  exit 0
fi

if [ ! -x "$ACP_PY" ]; then
  echo "SKIP  tests/bash/test-opencode-acp.sh (ACP venv missing: $ACP_PY)"
  exit 0
fi

if [ -f "$HOME/.config/rpp/litellm.env" ]; then
  # shellcheck disable=SC1090
  source "$HOME/.config/rpp/litellm.env"
fi

if [ -z "${LITELLM_API_KEY:-}" ] && [ -z "${LITELLM_MASTER_KEY:-}" ]; then
  echo "SKIP  tests/bash/test-opencode-acp.sh (LiteLLM env missing)"
  exit 0
fi

PROBE_DIR="$(mktemp -d /tmp/opencode-acp-test-XXXXXX)"
cleanup() {
  rm -rf "$PROBE_DIR"
}
trap cleanup EXIT

cp "$REPO_ROOT/configs/opencode/opencode.json" "$PROBE_DIR/opencode.json"

attempt=1
max_attempts=3
out=""
while [ "$attempt" -le "$max_attempts" ]; do
  out="$("$ACP_PY" "$PROBE" --cwd "$PROBE_DIR" --timeout 300 opencode acp 2>&1)" || {
    if [ "$attempt" -eq "$max_attempts" ]; then
      echo "FAIL  tests/bash/test-opencode-acp.sh"
      echo "$out" | sed 's/^/      /'
      exit 1
    fi
    attempt=$((attempt + 1))
    sleep 2
    continue
  }

  if printf '%s\n' "$out" | grep -q "stop_reason='end_turn'" \
    && printf '%s\n' "$out" | grep -Eq "text.*'4'|thoughts.*'4'"; then
    break
  fi

  if [ "$attempt" -eq "$max_attempts" ]; then
    if ! printf '%s\n' "$out" | grep -q "stop_reason='end_turn'"; then
      echo "FAIL  tests/bash/test-opencode-acp.sh (no end_turn)"
    else
      echo "FAIL  tests/bash/test-opencode-acp.sh (expected reply 4 in text or thoughts after ${max_attempts} attempts)"
    fi
    printf '%s\n' "$out" | sed 's/^/      /'
    exit 1
  fi
  attempt=$((attempt + 1))
  sleep 2
done

echo "PASS  tests/bash/test-opencode-acp.sh"
