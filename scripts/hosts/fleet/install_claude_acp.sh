#!/usr/bin/env bash
# Install the Claude Code ACP npm shim (claude-code-cli-acp) and wire ~/.local/bin.
# Claude has no native `claude acp` subcommand; goal-flight maps agent=claude to this binary.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v npm >/dev/null 2>&1; then
  for prefix in /opt/homebrew/bin /usr/local/bin; do
    if [[ -x "${prefix}/npm" ]]; then
      export PATH="${prefix}:${PATH}"
      break
    fi
  done
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "ERROR: npm required (install Node.js / Homebrew node)" >&2
  exit 1
fi

npm install -g claude-code-cli-acp
bash "${SCRIPT_DIR}/setup_worker_path.sh"

if command -v claude-code-cli-acp >/dev/null 2>&1; then
  echo "claude-code-cli-acp installed: $(command -v claude-code-cli-acp)"
  claude-code-cli-acp --version 2>/dev/null || npm list -g claude-code-cli-acp --depth=0 2>/dev/null || true
else
  echo "WARN: claude-code-cli-acp not on PATH after install; open a new shell or re-run setup_worker_path.sh" >&2
fi

echo ""
echo "Next: ensure Claude Code is signed in on this machine (claude or Claude.app first run)."
