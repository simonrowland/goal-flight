#!/usr/bin/env bash
# Tests for scripts/register-context-mode-codex.py.
#
# Critical: uses a sandboxed HOME so the real ~/.codex/config.toml is NEVER
# touched. Every assertion runs against a fresh tempdir.
#
# Implementation note: `mk_sandbox` sets `HOME` + `PATH` via globals so the
# parent shell sees the change. (Using `$(mk_sandbox …)` would lose the
# exports because $() spawns a subshell — earlier versions had this bug.)

set -eu

SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/scripts/register-context-mode-codex.py"
[ -x "$SCRIPT" ] || { echo "script not executable: $SCRIPT"; exit 1; }

command -v python3 >/dev/null 2>&1 || { echo "python3 missing; cannot run these tests"; exit 1; }

TMPROOT=$(mktemp -d /tmp/goal-flight-mirror-test-XXXXXX)
trap 'rm -rf "$TMPROOT"' EXIT

ORIG_PATH="$PATH"
ORIG_HOME="$HOME"

mk_sandbox() {
  local name="$1"
  SANDBOX="$TMPROOT/$name"
  mkdir -p "$SANDBOX/home" "$SANDBOX/bin"
  cat > "$SANDBOX/bin/codex" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
  chmod +x "$SANDBOX/bin/codex"
  cat > "$SANDBOX/bin/npx" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
  chmod +x "$SANDBOX/bin/npx"
  # Symlink the real python3 into the sandbox bin so test cases that strip
  # PATH down to $SANDBOX/bin still resolve the script's shebang correctly.
  # The script requires Python 3.11+ (for tomllib); fall back to "python3"
  # discovery via PATH if `which` doesn't find a 3.11+ binary.
  ln -sf "$(command -v python3)" "$SANDBOX/bin/python3"
  export HOME="$SANDBOX/home"
  export PATH="$SANDBOX/bin:$ORIG_PATH"
}

# --- test 1: --check with no codex on PATH → exit 0, "not installed" ---
mk_sandbox no-codex
# Remove the fake codex from sandbox bin; PATH (sandbox bin only) now lacks
# codex but still has the python3 symlink for the script's shebang.
rm "$SANDBOX/bin/codex"
out=$(PATH="$SANDBOX/bin" HOME="$HOME" "$SCRIPT" --check 2>&1) && rc=0 || rc=$?
[ "$rc" = "0" ] \
  || { echo "test1 FAIL: expected exit 0 when codex missing, got $rc; out=$out"; exit 1; }
echo "$out" | grep -q "codex not installed" \
  || { echo "test1 FAIL: expected 'codex not installed' line; got: $out"; exit 1; }
echo "test1 pass: --check with no codex exits 0 with skip message"

# --- test 2: --check with codex but no Claude install → exit 1 ---
mk_sandbox no-claude
out=$("$SCRIPT" --check 2>&1) && rc=0 || rc=$?
[ "$rc" = "1" ] \
  || { echo "test2 FAIL: expected exit 1 when Claude-side missing, got $rc; out=$out"; exit 1; }
echo "$out" | grep -qi "not detected claude-side" \
  || { echo "test2 FAIL: expected 'not detected Claude-side' line; got: $out"; exit 1; }
echo "test2 pass: --check reports MISSING when Claude install absent"

# --- test 3: --check with codex + Claude mcpServers entry, no codex config → exit 1 ---
mk_sandbox claude-explicit
cat > "$HOME/.claude.json" <<'EOF'
{
  "mcpServers": {
    "context-mode": {
      "command": "node",
      "args": ["/some/path/start.mjs"]
    }
  }
}
EOF
out=$("$SCRIPT" --check 2>&1) && rc=0 || rc=$?
[ "$rc" = "1" ] \
  || { echo "test3 FAIL: expected exit 1 when needs register, got $rc; out=$out"; exit 1; }
echo "$out" | grep -q "codex MISSING" \
  || { echo "test3 FAIL: expected 'codex MISSING' line; got: $out"; exit 1; }
echo "$out" | grep -q "mcpServers entry" \
  || { echo "test3 FAIL: expected provenance mention; got: $out"; exit 1; }
echo "test3 pass: --check detects mcpServers entry and reports MISSING"

# --- test 4: register from fresh state writes the block + exit 0 ---
mk_sandbox register-fresh
cat > "$HOME/.claude.json" <<'EOF'
{
  "mcpServers": {
    "context-mode": {
      "command": "node",
      "args": ["/some/path/start.mjs"]
    }
  }
}
EOF
out=$("$SCRIPT" 2>&1)
[ -f "$HOME/.codex/config.toml" ] \
  || { echo "test4 FAIL: codex config not created"; exit 1; }
grep -qF '[mcp_servers.context-mode]' "$HOME/.codex/config.toml" \
  || { echo "test4 FAIL: block missing"; cat "$HOME/.codex/config.toml"; exit 1; }
grep -qF '"-y"' "$HOME/.codex/config.toml" \
  || { echo "test4 FAIL: args[0] not -y"; cat "$HOME/.codex/config.toml"; exit 1; }
grep -qF '"context-mode@latest"' "$HOME/.codex/config.toml" \
  || { echo "test4 FAIL: args[1] not context-mode@latest"; cat "$HOME/.codex/config.toml"; exit 1; }
# stdout MUST report success — not "raced" (round-2 reviewer caught the
# Path() sentinel collision where fresh writes were misreported as raced).
echo "$out" | grep -q "registered context-mode for codex" \
  || { echo "test4 FAIL: stdout missing 'registered' success message"; echo "$out"; exit 1; }
echo "$out" | grep -qi "raced" \
  && { echo "test4 FAIL: stdout falsely reports 'raced' on fresh write"; echo "$out"; exit 1; }
echo "test4 pass: register writes canonical npx block (+ stdout success-reported, not raced-reported)"

# --- test 5: --check after register → exit 0 ---
"$SCRIPT" --check >/dev/null 2>&1 \
  || { echo "test5 FAIL: --check should exit 0 after register"; exit 1; }
echo "test5 pass: --check passes after register"

# --- test 6: idempotency — re-run does not duplicate ---
mk_sandbox idempotent
cat > "$HOME/.claude.json" <<'EOF'
{ "mcpServers": { "context-mode": { "command": "node", "args": [] } } }
EOF
"$SCRIPT" >/dev/null
size1=$(wc -c < "$HOME/.codex/config.toml")
"$SCRIPT" >/dev/null
size2=$(wc -c < "$HOME/.codex/config.toml")
[ "$size1" = "$size2" ] \
  || { echo "test6 FAIL: file size changed (was $size1, now $size2)"; exit 1; }
matches=$(grep -cF '[mcp_servers.context-mode]' "$HOME/.codex/config.toml")
[ "$matches" = "1" ] \
  || { echo "test6 FAIL: expected exactly 1 block, found $matches"; exit 1; }
echo "test6 pass: re-run is idempotent (no duplicate)"

# --- test 7: pre-existing codex block is preserved (no clobber) ---
mk_sandbox preserve
cat > "$HOME/.claude.json" <<'EOF'
{ "mcpServers": { "context-mode": { "command": "node", "args": [] } } }
EOF
mkdir -p "$HOME/.codex"
cat > "$HOME/.codex/config.toml" <<'EOF'
# user manually configured a custom command — should not be clobbered
[mcp_servers.context-mode]
command = "/my/custom/path/to/context-mode"
args = ["--config", "/etc/context-mode.toml"]
EOF
"$SCRIPT" >/dev/null
grep -qF '/my/custom/path/to/context-mode' "$HOME/.codex/config.toml" \
  || { echo "test7 FAIL: user's custom block was clobbered"; cat "$HOME/.codex/config.toml"; exit 1; }
matches=$(grep -cF '[mcp_servers.context-mode]' "$HOME/.codex/config.toml")
[ "$matches" = "1" ] \
  || { echo "test7 FAIL: created duplicate block; got $matches"; cat "$HOME/.codex/config.toml"; exit 1; }
echo "test7 pass: pre-existing codex block preserved (no clobber)"

# --- test 8: plugin-form detection ---
mk_sandbox plugin-form
mkdir -p "$HOME/.claude/plugins/cache/context-mode/1.0.103/.claude-plugin"
cat > "$HOME/.claude/plugins/cache/context-mode/1.0.103/.claude-plugin/plugin.json" <<'EOF'
{
  "name": "context-mode",
  "mcpServers": {
    "context-mode": {
      "command": "node",
      "args": ["${CLAUDE_PLUGIN_ROOT}/start.mjs"]
    }
  }
}
EOF
out=$("$SCRIPT" --check 2>&1) && rc=0 || rc=$?
[ "$rc" = "1" ] \
  || { echo "test8 FAIL: expected exit 1 (plugin detected, codex missing), got $rc; out=$out"; exit 1; }
echo "$out" | grep -q "plugin form at" \
  || { echo "test8 FAIL: expected plugin-form provenance; got: $out"; exit 1; }
"$SCRIPT" >/dev/null
grep -qF '"-y"' "$HOME/.codex/config.toml" \
  || { echo "test8 FAIL: register from plugin form didn't write npx args"; exit 1; }
echo "test8 pass: plugin-form detection + register"

# --- test 9: backup is created when codex config pre-existed ---
mk_sandbox backup-check
cat > "$HOME/.claude.json" <<'EOF'
{ "mcpServers": { "context-mode": { "command": "node", "args": [] } } }
EOF
mkdir -p "$HOME/.codex"
echo "# preexisting content" > "$HOME/.codex/config.toml"
"$SCRIPT" >/dev/null
backup_count=$(ls "$HOME/.codex"/config.toml.bak.* 2>/dev/null | wc -l | tr -d ' ')
[ "$backup_count" -ge "1" ] \
  || { echo "test9 FAIL: expected backup file; got $backup_count"; ls "$HOME/.codex/"; exit 1; }
ls "$HOME/.codex"/config.toml.bak.* | head -1 | xargs grep -qF "preexisting content" \
  || { echo "test9 FAIL: backup doesn't contain original content"; exit 1; }
echo "test9 pass: backup created with original content when codex config pre-existed"

# --- test 10: malformed Claude JSON does not crash; falls through ---
mk_sandbox bad-json
echo "{ this is not valid json" > "$HOME/.claude.json"
out=$("$SCRIPT" --check 2>&1) && rc=0 || rc=$?
[ "$rc" = "1" ] \
  || { echo "test10 FAIL: expected exit 1 (no Claude-side via valid json); got $rc; out=$out"; exit 1; }
echo "$out" | grep -qi "not detected claude-side" \
  || { echo "test10 FAIL: expected 'not detected' even with malformed JSON; got: $out"; exit 1; }
echo "test10 pass: malformed Claude JSON is handled gracefully"

# --- test 11: inline-table TOML form is detected as already-registered ---
# Round-2 P0: prior substring-match-based detection missed this form and
# would append a duplicate block, producing TOML with duplicate keys.
mk_sandbox inline-table
cat > "$HOME/.claude.json" <<'EOF'
{ "mcpServers": { "context-mode": { "command": "node", "args": [] } } }
EOF
mkdir -p "$HOME/.codex"
cat > "$HOME/.codex/config.toml" <<'EOF'
mcp_servers = { "context-mode" = { command = "node", args = [] } }
EOF
out=$("$SCRIPT" 2>&1)
echo "$out" | grep -q "already in" \
  || { echo "test11 FAIL: should detect inline-table form as already-registered; got: $out"; cat "$HOME/.codex/config.toml"; exit 1; }
# Verify we did NOT append a duplicate
matches=$(grep -cF 'context-mode' "$HOME/.codex/config.toml")
[ "$matches" = "1" ] \
  || { echo "test11 FAIL: expected 1 context-mode mention, found $matches"; cat "$HOME/.codex/config.toml"; exit 1; }
echo "test11 pass: inline-table TOML form detected (no duplicate append)"

# --- test 12: commented-out registration does NOT trigger false-positive no-op ---
# Round-2 P0: prior substring-match treated commented-out blocks as registered.
mk_sandbox commented-out
cat > "$HOME/.claude.json" <<'EOF'
{ "mcpServers": { "context-mode": { "command": "node", "args": [] } } }
EOF
mkdir -p "$HOME/.codex"
cat > "$HOME/.codex/config.toml" <<'EOF'
# This is a comment — historical reference
# [mcp_servers.context-mode]
# command = "old-form-no-longer-used"
EOF
"$SCRIPT" >/dev/null
# After registration, an ACTIVE block must exist (un-commented)
grep -qE '^\[mcp_servers\.context-mode\]' "$HOME/.codex/config.toml" \
  || { echo "test12 FAIL: commented-out should not block real registration"; cat "$HOME/.codex/config.toml"; exit 1; }
echo "test12 pass: commented-out registration does not block real append"

# --- test 13: non-dict Claude JSON (array, scalar) does not crash ---
# Round-2 P1: `cfg.get(...)` crashed when cfg was a list or string.
mk_sandbox non-dict-json
echo '["not", "a", "dict"]' > "$HOME/.claude.json"
out=$("$SCRIPT" --check 2>&1) && rc=0 || rc=$?
[ "$rc" = "1" ] \
  || { echo "test13a FAIL: expected exit 1 on non-dict JSON, got $rc; out=$out"; exit 1; }
# Also try scalar
echo '"just a string"' > "$HOME/.claude.json"
out=$("$SCRIPT" --check 2>&1) && rc=0 || rc=$?
[ "$rc" = "1" ] \
  || { echo "test13b FAIL: expected exit 1 on scalar JSON, got $rc; out=$out"; exit 1; }
echo "test13 pass: non-dict Claude JSON does not crash"

# --- test 14: missing npx produces clean error (no broken registration written) ---
# Round-2 P1: prior fallback `"/usr/bin/env npx"` would write an invalid command.
mk_sandbox no-npx
cat > "$HOME/.claude.json" <<'EOF'
{ "mcpServers": { "context-mode": { "command": "node", "args": [] } } }
EOF
# Remove npx from sandbox AND override PATH to exclude all real npx locations.
rm "$SANDBOX/bin/npx"
out=$(PATH="$SANDBOX/bin" "$SCRIPT" 2>&1) && rc=0 || rc=$?
[ "$rc" != "0" ] \
  || { echo "test14 FAIL: expected non-zero exit when npx missing, got $rc; out=$out"; exit 1; }
echo "$out" | grep -qi "npx" \
  || { echo "test14 FAIL: expected error mentioning npx; got: $out"; exit 1; }
[ ! -f "$HOME/.codex/config.toml" ] \
  || { echo "test14 FAIL: config.toml was written despite missing npx"; cat "$HOME/.codex/config.toml"; exit 1; }
echo "test14 pass: missing npx is a clean error (no broken registration)"

# --- test 15: lock file is cleaned up after successful register ---
# Round-2 P1: prior lock file persisted as a stale artifact.
mk_sandbox lock-cleanup
cat > "$HOME/.claude.json" <<'EOF'
{ "mcpServers": { "context-mode": { "command": "node", "args": [] } } }
EOF
"$SCRIPT" >/dev/null
[ ! -f "$HOME/.codex/.register-context-mode.lock" ] \
  || { echo "test15 FAIL: lock file persisted after successful register"; ls -la "$HOME/.codex/"; exit 1; }
echo "test15 pass: lock file cleaned up after register"

echo
echo "all register-context-mode-codex tests passed"
