#!/usr/bin/env bash
# Hermetic render checks for the standalone maintenance LaunchAgent.

set -eu

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPT="$ROOT/scripts/install-maintenance.sh"
TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/gf-maintenance-install-XXXXXX")"
trap 'rm -rf "$TMPROOT"' EXIT

HOME_DIR="$TMPROOT/home"
SKILL_ROOT="$TMPROOT/skill"
mkdir -p "$HOME_DIR" "$SKILL_ROOT"
out1="$TMPROOT/one.plist"
out2="$TMPROOT/two.plist"
HOME="$HOME_DIR" SKILL_ROOT="$SKILL_ROOT" PATH="$(dirname "$(command -v python3)"):/usr/bin:/bin" \
  "$SCRIPT" --dry-run > "$out1"
HOME="$HOME_DIR" SKILL_ROOT="$SKILL_ROOT" PATH="$(dirname "$(command -v python3)"):/usr/bin:/bin" \
  "$SCRIPT" --dry-run > "$out2"
cmp -s "$out1" "$out2"
grep -qF '<string>com.goalflight.maintenance</string>' "$out1"
grep -qF '<integer>3600</integer>' "$out1"
grep -qF '<string>--apply</string>' "$out1"
grep -qF '<string>--json</string>' "$out1"
grep -qF "$SKILL_ROOT/scripts/goalflight_maintenance.py" "$out1"
grep -qF 'maintenance-launchd.log' "$out1"
! grep -qF 'goalflight_dispatch.py' "$out1"
[ ! -e "$HOME_DIR/Library/LaunchAgents/com.goalflight.maintenance.plist" ]
echo "all install-maintenance tests passed"
