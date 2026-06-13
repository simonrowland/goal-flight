#!/usr/bin/env bash
# Install the Claude Code ACP npm shim (claude-code-cli-acp) and wire ~/.local/bin.
# Claude has no native `claude acp` subcommand; goal-flight maps agent=claude to this binary.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

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
bash "${REPO_ROOT}/scripts/install_claude_acp_patch.sh"

if command -v claude-code-cli-acp >/dev/null 2>&1; then
  echo "claude-code-cli-acp installed: $(command -v claude-code-cli-acp)"
  claude-code-cli-acp --version 2>/dev/null || npm list -g claude-code-cli-acp --depth=0 2>/dev/null || true
else
  echo "WARN: claude-code-cli-acp not on PATH after install; open a new shell or re-run setup_worker_path.sh" >&2
fi

echo ""
echo "Next (headless worker): mint a subscription token and persist it for ssh:"
echo "  claude setup-token        # interactive once; prints a long-lived token"
echo "  # add to this node's ~/.zshenv:  export CLAUDE_CODE_OAUTH_TOKEN=<token>"
echo "  claude auth status --json # expect authMethod=oauth_token / apiProvider=firstParty"
echo "  # (do NOT validate with 'claude -p' -- always API-billed)"
echo "On an interactive GUI machine, plain 'claude' / Claude.app first-run also works."
