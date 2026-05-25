#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
SETUP="$REPO_ROOT/setup.sh"

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
      exec "$SETUP" --cursor-install "$project" "$@"
      ;;
    opencode)
      shift
      if [[ $# -ge 1 && "$1" != -* ]]; then
        project=$1
        shift
      else
        project="."
      fi
      exec "$SETUP" --opencode-install "$project" "$@"
      ;;
    codex)
      shift
      exec "$SETUP" --codex-install "$@"
      ;;
  esac
fi

exec "$SETUP" "$@"
