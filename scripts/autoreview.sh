#!/usr/bin/env bash
# Goal Flight autoreview wrapper.
# - Default reviewer: Codex (vendored autoreview default).
# - Claude: route through claude-code-cli-acp via scripts/autoreview_claude_acp,
#   not the native headless Claude print CLI (API-billed path).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HELPER="${AUTOREVIEW_HELPER:-${ROOT}/autoreview/scripts/autoreview}"
CLAUDE_ACP="${ROOT}/scripts/autoreview_claude_acp"

if [[ ! -x "${HELPER}" ]]; then
  echo "autoreview helper not found: ${HELPER}" >&2
  echo "Vendored autoreview helper missing at autoreview/scripts/autoreview (override with AUTOREVIEW_HELPER if needed)." >&2
  exit 127
fi

if [[ ! -x "${CLAUDE_ACP}" ]]; then
  chmod +x "${CLAUDE_ACP}" 2>/dev/null || true
fi

use_claude_acp=false
has_engine=false
extra=()

for arg in "$@"; do
  case "${arg}" in
    --engine|--engine=*|--reviewers|--reviewers=*|--panel)
      use_claude_acp=true
      ;;
  esac
  if [[ "${arg}" == *claude* ]]; then
    use_claude_acp=true
  fi
  if [[ "${arg}" == --engine || "${arg}" == --engine=* ]]; then
    has_engine=true
  fi
done

if [[ "${use_claude_acp}" == true ]]; then
  extra+=(--claude-bin "${CLAUDE_ACP}")
fi

if [[ "${has_engine}" == false ]]; then
  extra+=(--engine codex)
fi

exec "${HELPER}" "${extra[@]}" "$@"
