#!/usr/bin/env bash
# Hermetic tests for scripts/install-drainer.sh. Never touches real launchctl
# or the real per-user LaunchAgents directory.

set -eu

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPT="$ROOT/scripts/install-drainer.sh"

[ -x "$SCRIPT" ] || { echo "script not executable: $SCRIPT"; exit 1; }

TMPROOT=$(mktemp -d /tmp/goal-flight-drainer-test-XXXXXX)
trap 'rm -rf "$TMPROOT"' EXIT

REAL_PYTHON="$(command -v python3)"
FAKEBIN="$TMPROOT/fakebin"
mkdir -p "$FAKEBIN"
ln -s "$REAL_PYTHON" "$FAKEBIN/python3"
EXPECTED_PYTHON="$FAKEBIN/python3"

SANDBOX_HOME="$TMPROOT/home"
SANDBOX_SKILL="$TMPROOT/skill"
mkdir -p "$SANDBOX_HOME" "$SANDBOX_SKILL"

render_once() {
  HOME="$SANDBOX_HOME" \
  SKILL_ROOT="$SANDBOX_SKILL" \
  PATH="$FAKEBIN:/usr/bin:/bin" \
  "$SCRIPT" --dry-run
}

out1="$TMPROOT/render-1.plist"
out2="$TMPROOT/render-2.plist"
render_once > "$out1"
render_once > "$out2"

cmp -s "$out1" "$out2" \
  || { echo "FAIL: dry-run render changed across identical invocations"; diff -u "$out1" "$out2" || true; exit 1; }
echo "test1 pass: dry-run render is idempotent"

grep -qF "<string>com.goalflight.drain</string>" "$out1" \
  || { echo "FAIL: Label missing"; cat "$out1"; exit 1; }
grep -qF "<string>$EXPECTED_PYTHON</string>" "$out1" \
  || { echo "FAIL: derived python path missing"; cat "$out1"; exit 1; }
grep -qF "<string>$SANDBOX_SKILL/scripts/goalflight_dispatch.py</string>" "$out1" \
  || { echo "FAIL: drain dispatch argv missing"; cat "$out1"; exit 1; }
grep -qF "<string>$SANDBOX_SKILL</string>" "$out1" \
  || { echo "FAIL: working directory missing"; cat "$out1"; exit 1; }
grep -qF "<string>$SANDBOX_HOME</string>" "$out1" \
  || { echo "FAIL: HOME env missing"; cat "$out1"; exit 1; }
grep -qF "<integer>60</integer>" "$out1" \
  || { echo "FAIL: StartInterval 60 missing"; cat "$out1"; exit 1; }
grep -qF "<string>drain</string>" "$out1" \
  || { echo "FAIL: drain argv token missing"; cat "$out1"; exit 1; }
grep -qF "<string>--json</string>" "$out1" \
  || { echo "FAIL: --json argv token missing"; cat "$out1"; exit 1; }
grep -qF "<string>$SANDBOX_HOME/.goal-flight/drain-launchd.log</string>" "$out1" \
  || { echo "FAIL: log path missing"; cat "$out1"; exit 1; }
echo "test2 pass: rendered plist contains expected launchd fields"

if grep -q '@[A-Z_][A-Z_]*@' "$out1"; then
  echo "FAIL: leftover template token found"
  grep -n '@[A-Z_][A-Z_]*@' "$out1"
  exit 1
fi
echo "test3 pass: no leftover template tokens"

[ ! -e "$SANDBOX_HOME/Library/LaunchAgents/com.goalflight.drain.plist" ] \
  || { echo "FAIL: dry-run wrote LaunchAgents plist"; exit 1; }
[ ! -e "$SANDBOX_HOME/.goal-flight/drain-launchd.log" ] \
  || { echo "FAIL: dry-run touched log path"; exit 1; }
echo "test4 pass: dry-run has no filesystem side effects under HOME"

scan_files="
scripts/install-drainer.sh
scripts/templates/com.goalflight.drain.plist.tmpl
protocols/drainer.md
protocols/README.md
tests/bash/test-install-drainer.sh
"

needle='/'"Users"'/[A-Za-z0-9._-][A-Za-z0-9._-]*'
hits=""
for file in $scan_files; do
  if grep -nE "$needle" "$ROOT/$file" >/tmp/goal-flight-drainer-path-hits.$$ 2>/dev/null; then
    hits="${hits}${file}:$(cat /tmp/goal-flight-drainer-path-hits.$$)
"
  fi
done
rm -f /tmp/goal-flight-drainer-path-hits.$$

[ -z "$hits" ] || { echo "FAIL: portable drainer files contain machine-specific user paths"; printf '%s' "$hits"; exit 1; }
echo "test5 pass: portable drainer files contain no machine-specific user paths"

echo
echo "all install-drainer tests passed"
