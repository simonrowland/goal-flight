#!/usr/bin/env bash
# Branch tests for scripts/install_claude_acp_patch.sh.

set -eu

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPT="$ROOT/scripts/install_claude_acp_patch.sh"
[ -x "$SCRIPT" ] || { echo "script not executable: $SCRIPT"; exit 1; }

TMPROOT="$(mktemp -d /tmp/goal-flight-claude-acp-patch-test-XXXXXX)"
trap 'rm -rf "$TMPROOT"' EXIT

target="$TMPROOT/claude-code-cli-acp"
patched="$TMPROOT/patched-claude-code-cli-acp"
printf 'original\n' > "$target"
printf 'patched\n' > "$patched"
chmod 755 "$target" "$patched"

out=$(
  GOALFLIGHT_CLAUDE_ACP_VERSION=0.1.2 \
  GOALFLIGHT_CLAUDE_ACP_FORCE_CARGO_MISSING=1 \
  "$SCRIPT" 2>&1
) && rc=0 || rc=$?
[ "$rc" = "0" ] || { echo "newer-version FAIL: rc=$rc out=$out"; exit 1; }
echo "$out" | grep -q "newer than 0.1.1" \
  || { echo "newer-version FAIL: expected stopgap skip; out=$out"; exit 1; }
echo "newer-version pass"

out=$(
  GOALFLIGHT_CLAUDE_ACP_VERSION=0.1.1 \
  GOALFLIGHT_CLAUDE_ACP_BIN_PATH="$target" \
  GOALFLIGHT_CLAUDE_ACP_FORCE_CARGO_MISSING=1 \
  "$SCRIPT" 2>&1
) && rc=0 || rc=$?
[ "$rc" = "3" ] || { echo "cargo-absent FAIL: expected rc=3 got $rc out=$out"; exit 1; }
echo "$out" | grep -q "Rust cargo is required" \
  || { echo "cargo-absent FAIL: expected cargo message; out=$out"; exit 1; }
echo "cargo-absent pass"

cp "$patched" "$target"
out=$(
  GOALFLIGHT_CLAUDE_ACP_VERSION=0.1.1 \
  GOALFLIGHT_CLAUDE_ACP_BIN_PATH="$target" \
  GOALFLIGHT_CLAUDE_ACP_PREBUILT_BINARY="$patched" \
  "$SCRIPT" 2>&1
) && rc=0 || rc=$?
[ "$rc" = "0" ] || { echo "already-patched FAIL: rc=$rc out=$out"; exit 1; }
echo "$out" | grep -q "already matches the patched build" \
  || { echo "already-patched FAIL: expected idempotent skip; out=$out"; exit 1; }
echo "already-patched pass"

fakebin="$TMPROOT/fakebin"
mkdir "$fakebin"
printf '#!/usr/bin/env sh\nprintf "Darwin\\n"\n' > "$fakebin/uname"
chmod 755 "$fakebin/uname"

for failing_tool in xattr codesign; do
  printf 'current-%s\n' "$failing_tool" > "$target"
  printf 'stale-%s\n' "$failing_tool" > "$target.orig"
  chmod 755 "$target" "$target.orig"
  if [ "$failing_tool" = "xattr" ]; then
    printf '#!/usr/bin/env sh\nexit 1\n' > "$fakebin/xattr"
    printf '#!/usr/bin/env sh\nexit 0\n' > "$fakebin/codesign"
  else
    printf '#!/usr/bin/env sh\nexit 0\n' > "$fakebin/xattr"
    printf '#!/usr/bin/env sh\nexit 1\n' > "$fakebin/codesign"
  fi
  chmod 755 "$fakebin/xattr" "$fakebin/codesign"
  out=$(
    PATH="$fakebin:$PATH" \
    GOALFLIGHT_CLAUDE_ACP_VERSION=0.1.1 \
    GOALFLIGHT_CLAUDE_ACP_BIN_PATH="$target" \
    GOALFLIGHT_CLAUDE_ACP_PREBUILT_BINARY="$patched" \
    "$SCRIPT" 2>&1
  ) && rc=0 || rc=$?
  [ "$rc" = "2" ] || { echo "rollback-$failing_tool FAIL: expected rc=2 got $rc out=$out"; exit 1; }
  [ "$(cat "$target")" = "current-$failing_tool" ] \
    || { echo "rollback-$failing_tool FAIL: target was not restored from run backup; target=$(cat "$target") out=$out"; exit 1; }
  [ "$(cat "$target.orig")" = "stale-$failing_tool" ] \
    || { echo "rollback-$failing_tool FAIL: persistent .orig changed; orig=$(cat "$target.orig") out=$out"; exit 1; }
  echo "$out" | grep -q "run-scoped backup" \
    || { echo "rollback-$failing_tool FAIL: expected run-scoped restore message; out=$out"; exit 1; }
  echo "rollback-$failing_tool pass"
done
