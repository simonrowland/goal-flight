#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
SETUP="$REPO_ROOT/setup.sh"
WORKER_PATH="${REPO_ROOT}/scripts/hosts/fleet/setup_worker_path.sh"

ensure_mac_worker_bins() {
  if [[ "$(uname -s)" == Darwin && -x "${WORKER_PATH}" ]]; then
    bash "${WORKER_PATH}"
  fi
}

if [[ $# -ge 1 ]]; then
  case "$1" in
    cursor)
      shift
      if [[ $# -ge 1 && "$1" != -* ]]; then
        project=$1
        shift
      else
        project="."
      fi
      "$SETUP" --cursor-install "$project" "$@"
      ensure_mac_worker_bins
      exit $?
      ;;
    opencode)
      shift
      if [[ $# -ge 1 && "$1" != -* ]]; then
        project=$1
        shift
      else
        project="."
      fi
      "$SETUP" --opencode-install "$project" "$@"
      ensure_mac_worker_bins
      exit $?
      ;;
    codex)
      shift
      "$SETUP" --codex-install "$@"
      ensure_mac_worker_bins
      exit $?
      ;;
    grok)
      shift
      bash "${REPO_ROOT}/scripts/hosts/fleet/install_grok.sh" "$@"
      exit $?
      ;;
    claude-acp)
      shift
      bash "${REPO_ROOT}/scripts/hosts/fleet/install_claude_acp.sh" "$@"
      exit $?
      ;;
    worker-path)
      shift
      ensure_mac_worker_bins
      exit $?
      ;;
  esac
fi

exec "$SETUP" "$@"
