#!/usr/bin/env bash
# Ensure ~/.goal-flight/venvs/acp-0.10 exists for remote fleet ACP dispatch.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
ACP_VENV="${HOME}/.goal-flight/venvs/acp-0.10"
REQ="${REPO_ROOT}/requirements.txt"

if [[ ! -f "${REQ}" ]]; then
  echo "ERROR: requirements.txt missing at ${REQ}" >&2
  exit 1
fi

if [[ -x "${ACP_VENV}/bin/python" ]]; then
  if "${ACP_VENV}/bin/python" -c "import acp" 2>/dev/null; then
    echo "ok: ACP venv ready at ${ACP_VENV}"
    exit 0
  fi
fi

mkdir -p "${HOME}/.goal-flight/venvs"
if command -v uv >/dev/null 2>&1; then
  uv venv "${ACP_VENV}"
  uv pip install --python "${ACP_VENV}/bin/python" -r "${REQ}"
else
  python3 -m venv "${ACP_VENV}"
  "${ACP_VENV}/bin/python" -m pip install -r "${REQ}"
fi

"${ACP_VENV}/bin/python" -c "import acp; print('ok: ACP venv ready at ${ACP_VENV}')"
