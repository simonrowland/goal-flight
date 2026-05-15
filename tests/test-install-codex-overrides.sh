#!/usr/bin/env bash
# Tests for scripts/install-codex-overrides.sh.
#
# Critical: uses a sandboxed HOME so the real ~/.codex/config.toml is NEVER
# touched. Every assertion runs against a fresh tempdir.

set -eu

SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/scripts/install-codex-overrides.sh"
[ -x "$SCRIPT" ] || { echo "script not executable: $SCRIPT"; exit 1; }

TMPROOT=$(mktemp -d /tmp/goal-flight-test-XXXXXX)
trap 'rm -rf "$TMPROOT"' EXIT

# Sandbox: every test exports HOME=$TMPROOT/<test-name>/home, so the script
# writes to a per-test ~/.codex/config.toml that auto-cleans on exit.
mk_sandbox() {
  local name="$1"
  local sandbox="$TMPROOT/$name"
  local proj="$sandbox/proj"
  mkdir -p "$sandbox/home" "$proj"
  ( cd "$proj" && git init -q )
  export HOME="$sandbox/home"
  echo "$proj"
}

# --- test 1: --check on fresh state reports MISSING + exit 1 ---
proj=$(mk_sandbox check-fresh)
out=$("$SCRIPT" --check "$proj" 2>&1) && rc=0 || rc=$?
if [ "$rc" -ne 1 ]; then
  echo "test1 FAIL: expected exit 1 on missing-trust --check, got $rc"
  exit 1
fi
echo "$out" | grep -q "CHECK: user config does NOT trust" \
  || { echo "test1 FAIL: expected MISSING line"; echo "$out"; exit 1; }
echo "test1 pass: --check reports MISSING on fresh state"

# --- test 2: install on fresh state adds entry + exit 0 ---
proj=$(mk_sandbox install-fresh)
out=$("$SCRIPT" "$proj" 2>&1)
[ -f "$HOME/.codex/config.toml" ] \
  || { echo "test2 FAIL: user config not created"; exit 1; }
grep -qF "[projects.\"$proj\"]" "$HOME/.codex/config.toml" \
  || { echo "test2 FAIL: trust block missing from user config"; cat "$HOME/.codex/config.toml"; exit 1; }
grep -qF 'trust_level = "trusted"' "$HOME/.codex/config.toml" \
  || { echo "test2 FAIL: trust_level not 'trusted'"; exit 1; }
echo "test2 pass: install adds trust block"

# --- test 3: --check now passes ---
"$SCRIPT" --check "$proj" >/dev/null 2>&1 \
  || { echo "test3 FAIL: --check should exit 0 after install"; exit 1; }
echo "test3 pass: --check passes after install"

# --- test 4: idempotency — re-run leaves file unchanged ---
proj=$(mk_sandbox idempotent)
"$SCRIPT" "$proj" >/dev/null
size1=$(wc -c < "$HOME/.codex/config.toml")
"$SCRIPT" "$proj" >/dev/null
size2=$(wc -c < "$HOME/.codex/config.toml")
[ "$size1" = "$size2" ] \
  || { echo "test4 FAIL: file size changed on re-run (was $size1, now $size2)"; exit 1; }
# Belt-and-braces: also assert exactly one matching block
matches=$(grep -cF "[projects.\"$proj\"]" "$HOME/.codex/config.toml")
[ "$matches" = "1" ] \
  || { echo "test4 FAIL: expected exactly 1 trust block, found $matches"; exit 1; }
echo "test4 pass: re-run is idempotent"

# --- test 5: project mirror written by default ---
proj=$(mk_sandbox mirror-default)
"$SCRIPT" "$proj" >/dev/null
[ -f "$proj/.codex/config.toml" ] \
  || { echo "test5 FAIL: project mirror not written"; exit 1; }
grep -qF "trust_level" "$proj/.codex/config.toml" \
  || { echo "test5 FAIL: project mirror missing trust_level"; cat "$proj/.codex/config.toml"; exit 1; }
echo "test5 pass: project mirror written by default"

# --- test 6: --no-project-mirror skips project file ---
proj=$(mk_sandbox no-mirror)
"$SCRIPT" --no-project-mirror "$proj" >/dev/null
[ ! -f "$proj/.codex/config.toml" ] \
  || { echo "test6 FAIL: project mirror written despite --no-project-mirror"; exit 1; }
# But user config should still have the entry
grep -qF "[projects.\"$proj\"]" "$HOME/.codex/config.toml" \
  || { echo "test6 FAIL: user config still missing trust block"; exit 1; }
echo "test6 pass: --no-project-mirror skips project file"

# --- test 7: pre-existing project mirror with the entry is not duplicated ---
proj=$(mk_sandbox mirror-existing)
mkdir -p "$proj/.codex"
cat > "$proj/.codex/config.toml" <<EOF
# pre-existing project config
sandbox_mode = "read-only"

[projects."$proj"]
trust_level = "trusted"
EOF
"$SCRIPT" "$proj" >/dev/null
matches=$(grep -cF "[projects.\"$proj\"]" "$proj/.codex/config.toml")
[ "$matches" = "1" ] \
  || { echo "test7 FAIL: project mirror trust block duplicated ($matches matches)"; cat "$proj/.codex/config.toml"; exit 1; }
echo "test7 pass: pre-existing project mirror not duplicated"

# --- test 8: pre-existing project mirror WITHOUT the entry gets appended to ---
proj=$(mk_sandbox mirror-append)
mkdir -p "$proj/.codex"
cat > "$proj/.codex/config.toml" <<EOF
# pre-existing project config without trust block
sandbox_mode = "read-only"
EOF
"$SCRIPT" "$proj" >/dev/null
grep -qF "[projects.\"$proj\"]" "$proj/.codex/config.toml" \
  || { echo "test8 FAIL: trust block not appended to existing mirror"; cat "$proj/.codex/config.toml"; exit 1; }
# Original sandbox_mode line should still be there
grep -qF 'sandbox_mode = "read-only"' "$proj/.codex/config.toml" \
  || { echo "test8 FAIL: original mirror content lost"; exit 1; }
echo "test8 pass: existing mirror gets trust block appended"

echo
echo "all install-codex-overrides tests passed"
