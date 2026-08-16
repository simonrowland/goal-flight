#!/usr/bin/env bash
# Hermetic render tests for the fleet-console launchd producer installer.

set -eu

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPT="$ROOT/scripts/install-fleet-console.sh"
TMPROOT=$(mktemp -d /tmp/goal-flight-fleet-console-install-test-XXXXXX)
trap 'rm -rf "$TMPROOT"' EXIT

REAL_PYTHON="$(command -v python3)"
FAKEBIN="$TMPROOT/fakebin"
mkdir -p "$FAKEBIN"
ln -s "$REAL_PYTHON" "$FAKEBIN/python3"

SANDBOX_HOME="$TMPROOT/home"
SANDBOX_SKILL="$TMPROOT/skill"
mkdir -p "$SANDBOX_HOME" "$SANDBOX_SKILL"

render_plane() {
  HOME="$SANDBOX_HOME" \
  SKILL_ROOT="$SANDBOX_SKILL" \
  PATH="$FAKEBIN:/usr/bin:/bin" \
  bash "$SCRIPT" --dry-run --plane "$1"
}

attention="$TMPROOT/attention.plist"
fleet="$TMPROOT/fleet.plist"
render_plane attention > "$attention"
render_plane fleet > "$fleet"

grep -qF '<string>com.goalflight.fleet-console.attention</string>' "$attention"
grep -qF '<integer>20</integer>' "$attention"
grep -qF '<string>--interval-s</string>' "$attention"
grep -qF '<string>20</string>' "$attention"
grep -qF '<string>10</string>' "$attention"
grep -qF "<string>$SANDBOX_SKILL/templates/fleet-console/attention-data.js</string>" "$attention"
grep -qF '<string>com.goalflight.fleet-console.fleet</string>' "$fleet"
grep -qF '<integer>60</integer>' "$fleet"
grep -qF '<string>--interval-s</string>' "$fleet"
grep -qF '<string>60</string>' "$fleet"
grep -qF '<string>30</string>' "$fleet"
grep -qF "<string>$SANDBOX_SKILL/templates/fleet-console/fleet-data.js</string>" "$fleet"
echo "test1 pass: separate launchd planes render their documented cadence and budget"

"$REAL_PYTHON" - "$attention" "$fleet" <<'PY'
import plistlib
import sys
from pathlib import Path

for path, expected in zip(map(Path, sys.argv[1:]), (20, 60)):
    payload = plistlib.loads(path.read_bytes())
    argv = payload["ProgramArguments"]
    forwarded = int(argv[argv.index("--interval-s") + 1])
    assert payload["StartInterval"] == forwarded == expected
PY
echo "test1b pass: each plist StartInterval reaches its producer argv unchanged"

for rendered in "$attention" "$fleet"; do
  grep -qF "<string>$FAKEBIN/python3</string>" "$rendered"
  grep -qF "<string>$SANDBOX_SKILL/scripts/goalflight_fleet_console_producer.py</string>" "$rendered"
  grep -qF "<string>$SANDBOX_HOME/.goal-flight/locks</string>" "$rendered"
  grep -qF '<key>RunAtLoad</key>' "$rendered"
  if grep -q '@[A-Z_][A-Z_]*@' "$rendered"; then
    echo "FAIL: leftover template token in $rendered"
    exit 1
  fi
done
echo "test2 pass: tick argv, lock directory, RunAtLoad, and substitutions are complete"

[ ! -e "$SANDBOX_HOME/Library/LaunchAgents" ]
[ ! -e "$SANDBOX_HOME/.goal-flight" ]
echo "test3 pass: dry-run neither installs nor starts launchd jobs"

if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$attention" >/dev/null
  plutil -lint "$fleet" >/dev/null
fi
echo "test4 pass: each rendered plist is valid"

printf '%s\n' '#!/bin/sh' 'if [ "${1:-}" = help ]; then echo bootstrap; fi' 'exit 0' > "$FAKEBIN/launchctl"
chmod +x "$FAKEBIN/launchctl"
HOME="$SANDBOX_HOME" \
SKILL_ROOT="$SANDBOX_SKILL" \
PATH="$FAKEBIN:/usr/bin:/bin" \
bash "$SCRIPT" --plane fleet > "$TMPROOT/install.log"
CONFIG_PATH="$SANDBOX_HOME/.goal-flight/fleet-console-output-dir"
grep -qF "$SANDBOX_SKILL/templates/fleet-console" "$CONFIG_PATH"
HOME="$SANDBOX_HOME" \
SKILL_ROOT="$SANDBOX_SKILL" \
PATH="$FAKEBIN:/usr/bin:/bin" \
bash "$SCRIPT" --uninstall > "$TMPROOT/uninstall.log"
[ ! -e "$CONFIG_PATH" ]
echo "test5 pass: install opts history hooks in and full uninstall opts them out"

echo
echo "all install-fleet-console tests passed"
