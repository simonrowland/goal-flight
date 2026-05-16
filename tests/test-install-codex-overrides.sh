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

# Sandbox: every test sets HOME=$TMPROOT/<test-name>/home, so the script
# writes to a per-test ~/.codex/config.toml that auto-cleans on exit.
#
# Implementation note: `mk_sandbox` sets HOME via globals so the parent shell
# sees the change. Earlier versions used `proj=$(mk_sandbox …)` which loses
# the export because $() spawns a subshell — silently writing to the real
# ~/.codex/config.toml.
mk_sandbox() {
  local name="$1"
  SANDBOX="$TMPROOT/$name"
  PROJ="$SANDBOX/proj"
  mkdir -p "$SANDBOX/home" "$PROJ"
  ( cd "$PROJ" && git init -q )
  export HOME="$SANDBOX/home"
}

# --- test 1: --check on fresh state reports MISSING + exit 1 ---
mk_sandbox check-fresh; proj="$PROJ"
out=$("$SCRIPT" --check "$proj" 2>&1) && rc=0 || rc=$?
if [ "$rc" -ne 1 ]; then
  echo "test1 FAIL: expected exit 1 on missing-trust --check, got $rc"
  exit 1
fi
echo "$out" | grep -qE "CHECK: (user config does NOT trust|.* does not exist)" \
  || { echo "test1 FAIL: expected MISSING line"; echo "$out"; exit 1; }
echo "test1 pass: --check reports MISSING on fresh state"

# --- test 2: install on fresh state adds entry + exit 0 ---
mk_sandbox install-fresh; proj="$PROJ"
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
mk_sandbox idempotent; proj="$PROJ"
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
mk_sandbox mirror-default; proj="$PROJ"
"$SCRIPT" "$proj" >/dev/null
[ -f "$proj/.codex/config.toml" ] \
  || { echo "test5 FAIL: project mirror not written"; exit 1; }
grep -qF "trust_level" "$proj/.codex/config.toml" \
  || { echo "test5 FAIL: project mirror missing trust_level"; cat "$proj/.codex/config.toml"; exit 1; }
echo "test5 pass: project mirror written by default"

# --- test 6: --no-project-mirror skips project file ---
mk_sandbox no-mirror; proj="$PROJ"
"$SCRIPT" --no-project-mirror "$proj" >/dev/null
[ ! -f "$proj/.codex/config.toml" ] \
  || { echo "test6 FAIL: project mirror written despite --no-project-mirror"; exit 1; }
# But user config should still have the entry
grep -qF "[projects.\"$proj\"]" "$HOME/.codex/config.toml" \
  || { echo "test6 FAIL: user config still missing trust block"; exit 1; }
echo "test6 pass: --no-project-mirror skips project file"

# --- test 7: pre-existing project mirror with the entry is not duplicated ---
mk_sandbox mirror-existing; proj="$PROJ"
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
mk_sandbox mirror-append; proj="$PROJ"
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

# --- test 9: install auto-appends .codex/ to project .gitignore ---
mk_sandbox gitignore-auto; proj="$PROJ"
"$SCRIPT" "$proj" >/dev/null
[ -f "$proj/.gitignore" ] \
  || { echo "test9 FAIL: .gitignore not created"; exit 1; }
grep -qxF '.codex/' "$proj/.gitignore" \
  || { echo "test9 FAIL: .gitignore does not list '.codex/'"; cat "$proj/.gitignore"; exit 1; }
echo "test9 pass: install auto-appends .codex/ to .gitignore (fresh)"

# --- test 10: existing .gitignore is preserved; .codex/ is appended ---
mk_sandbox gitignore-existing; proj="$PROJ"
cat > "$proj/.gitignore" <<'EOF'
node_modules/
*.log
EOF
"$SCRIPT" "$proj" >/dev/null
grep -qxF 'node_modules/' "$proj/.gitignore" \
  || { echo "test10 FAIL: pre-existing .gitignore content lost"; cat "$proj/.gitignore"; exit 1; }
grep -qxF '.codex/' "$proj/.gitignore" \
  || { echo "test10 FAIL: .codex/ not appended"; cat "$proj/.gitignore"; exit 1; }
echo "test10 pass: existing .gitignore preserved; .codex/ appended"

# --- test 11: re-run does not duplicate .codex/ in .gitignore ---
"$SCRIPT" "$proj" >/dev/null
codex_count=$(grep -cxF '.codex/' "$proj/.gitignore")
[ "$codex_count" = "1" ] \
  || { echo "test11 FAIL: expected exactly 1 '.codex/' line, found $codex_count"; cat "$proj/.gitignore"; exit 1; }
echo "test11 pass: re-run is idempotent for .gitignore"

# --- test 12: --no-project-mirror skips gitignore too ---
mk_sandbox gitignore-skip; proj="$PROJ"
"$SCRIPT" --no-project-mirror "$proj" >/dev/null
# .gitignore should not be created OR if pre-existing, should not gain .codex/
if [ -f "$proj/.gitignore" ]; then
  grep -qxF '.codex/' "$proj/.gitignore" \
    && { echo "test12 FAIL: .codex/ added to .gitignore despite --no-project-mirror"; exit 1; }
fi
echo "test12 pass: --no-project-mirror skips gitignore"

echo
echo "all install-codex-overrides tests passed"
