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
  if ! uv venv --clear "${ACP_VENV}"; then
    rm -rf "${ACP_VENV}"
    uv venv "${ACP_VENV}"
  fi
  uv pip install --python "${ACP_VENV}/bin/python" -r "${REQ}"
else
  if [[ -n "${GOALFLIGHT_PYTHON:-}" ]]; then
    GF_PY="$GOALFLIGHT_PYTHON"
  else
    GF_PY_CANDIDATE="python${GOALFLIGHT_PYTHON_MAJOR:-3}"
    if command -v "$GF_PY_CANDIDATE" >/dev/null 2>&1; then
      GF_PY="$GF_PY_CANDIDATE"
    else
      GF_PY="python"
    fi
  fi
  rm -rf "${ACP_VENV}"
  "$GF_PY" -m venv "${ACP_VENV}"
  "${ACP_VENV}/bin/python" -m pip install -r "${REQ}"
fi

"${ACP_VENV}/bin/python" -c "import acp; print('ok: ACP venv ready at ${ACP_VENV}')"
