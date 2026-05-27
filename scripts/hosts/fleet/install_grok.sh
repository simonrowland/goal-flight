#!/usr/bin/env bash
# Install Grok Build CLI and wire PATH for fleet workers.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SETUP_PATH="${SCRIPT_DIR}/setup_worker_path.sh"

curl -fsSL https://x.ai/cli/install.sh | bash
bash "${SETUP_PATH}"

if command -v grok >/dev/null 2>&1; then
  echo "grok installed: $(command -v grok)"
  grok --version 2>/dev/null || true
else
  echo "WARN: grok not on PATH after install; open a new shell or run setup_worker_path.sh" >&2
fi

echo ""
echo "Next: sign in (SuperGrok or X Premium Plus required):"
echo "  grok login --oauth          # browser on this machine"
echo "  grok login --device-auth    # headless / SSH: visit URL + enter code"
