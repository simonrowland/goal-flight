#!/usr/bin/env bash
set -euo pipefail

# Commit with a file-backed message and explicit pathspecs.

usage() {
  echo "usage: scripts/goalflight_commit.sh <msg-file> -- <files...>" >&2
  exit 1
}

if [[ $# -lt 3 ]]; then
  usage
fi

msg_file=$1
shift

if [[ ! -f "$msg_file" ]]; then
  echo "message file not found: $msg_file" >&2
  usage
fi

if [[ "${1:-}" != "--" ]]; then
  echo "missing -- separator" >&2
  usage
fi
shift

if [[ $# -lt 1 ]]; then
  usage
fi

git commit -F "$msg_file" -- "$@"
