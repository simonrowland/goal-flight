#!/usr/bin/env bash
# Gated real WSL smoke. Skips everywhere unless GOALFLIGHT_WSL=1 and this
# process is actually inside WSL. Native Windows cannot run this bash/POSIX
# dispatch path; Monday acceptance should open the installed distro first.

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [ "${GOALFLIGHT_WSL:-}" != "1" ]; then
  echo "SKIP: set GOALFLIGHT_WSL=1 inside WSL to run dispatch smoke"
  exit 0
fi

if ! { grep -qiE 'microsoft|wsl' /proc/sys/kernel/osrelease 2>/dev/null || grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; }; then
  # If GOALFLIGHT_WSL=1 still skips on real WSL, inspect these two files first;
  # WSL1/WSL2 should expose a Microsoft/WSL marker in osrelease or version.
  echo "SKIP: GOALFLIGHT_WSL=1 set but kernel does not look like WSL"
  exit 0
fi

tmp="${TMPDIR:-/tmp}/goalflight-wsl-smoke-$$"
rm -rf "$tmp"
mkdir -p "$tmp"
trap 'rm -rf "$tmp"' EXIT

status="$tmp/status.json"
tail="$tmp/tail.log"
state_dir="$tmp/state"

GOALFLIGHT_STATE_DIR="$state_dir" python3 "$REPO_ROOT/scripts/goalflight_dispatch.py" \
  --shape bash \
  --agent wsl-smoke \
  --cwd "$REPO_ROOT" \
  --dispatch-id "wsl-smoke-$$" \
  --tail "$tail" \
  --status-json "$status" \
  --poll-secs 0.2 \
  --max-idle-secs 20 \
  -- \
  python3 -c 'print("STATUS: wsl smoke"); print("COMPLETE: wsl smoke")'
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "dispatch failed rc=$rc"
  [ -f "$tail" ] && sed -n '1,80p' "$tail"
  [ -f "$status" ] && sed -n '1,80p' "$status"
  exit "$rc"
fi

python3 - "$status" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    payload = json.load(fh)

if payload.get("state") != "complete":
    raise SystemExit(f"expected complete, got {payload.get('state')}: {payload}")

marker = payload.get("terminal_marker") or {}
if marker.get("kind") != "COMPLETE":
    raise SystemExit(f"expected COMPLETE marker, got {marker}")
PY
validator_rc=$?
if [ "$validator_rc" -ne 0 ]; then
  echo "status validator failed rc=$validator_rc"
  [ -f "$status" ] && sed -n '1,80p' "$status"
  exit "$validator_rc"
fi

echo "OK: WSL dispatch smoke passed"
