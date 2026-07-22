#!/usr/bin/env bash
# Hermetic tests for scripts/hosts/opencode/register_context_mode.py.

set -eu

SCRIPT="$(cd "$(dirname "$0")/../.." && pwd)/scripts/hosts/opencode/register_context_mode.py"
[ -x "$SCRIPT" ] || { echo "script not executable: $SCRIPT"; exit 1; }

command -v python3 >/dev/null 2>&1 || { echo "python3 missing; cannot run these tests"; exit 1; }

TMPROOT=$(mktemp -d /tmp/goal-flight-opencode-mirror-test-XXXXXX)
trap 'rm -rf "$TMPROOT"' EXIT

ORIG_PATH="$PATH"
REAL_PYTHON3="$(command -v python3)"
[ -x "$REAL_PYTHON3" ] || { echo "no python3 found"; exit 1; }

mk_sandbox() {
  local name="$1"
  SANDBOX="$TMPROOT/$name"
  mkdir -p "$SANDBOX/home/.config/opencode" "$SANDBOX/bin"
  cat > "$SANDBOX/bin/opencode" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  --version) echo "opencode test 0.0.0"; exit 0 ;;
  acp)
    if [ "$2" = "--help" ]; then exit 0; fi
    ;;
esac
exit 0
EOF
  chmod +x "$SANDBOX/bin/opencode"
  cat > "$SANDBOX/bin/npx" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
  chmod +x "$SANDBOX/bin/npx"
  ln -sf "$REAL_PYTHON3" "$SANDBOX/bin/python3"
  export HOME="$SANDBOX/home"
  export PATH="$SANDBOX/bin:$ORIG_PATH"
  unset LITELLM_API_KEY LITELLM_MASTER_KEY LITELLM_BASE_URL LITELLM_OPENCODE_MODEL
}

# --- test 1: register fresh writes permissions + MCP, not LiteLLM by default ---
mk_sandbox register-fresh
out=$("$SCRIPT" --scope global 2>&1)
[ -f "$HOME/.config/opencode/opencode.json" ] \
  || { echo "test1 FAIL: config not created"; exit 1; }
grep -q 'context-mode' "$HOME/.config/opencode/opencode.json" \
  || { echo "test1 FAIL: MCP missing"; cat "$HOME/.config/opencode/opencode.json"; exit 1; }
grep -q 'goal-flight' "$HOME/.config/opencode/opencode.json" \
  || { echo "test1 FAIL: skill permission missing"; cat "$HOME/.config/opencode/opencode.json"; exit 1; }
grep -q 'opencode-plugin-litellm' "$HOME/.config/opencode/opencode.json" \
  && { echo "test1 FAIL: LiteLLM profile merged without credentials"; cat "$HOME/.config/opencode/opencode.json"; exit 1; }
echo "$out" | grep -q 'registered goal-flight OpenCode config' \
  || { echo "test1 FAIL: missing success message"; echo "$out"; exit 1; }
echo "test1 pass: register writes MCP and permissions only by default"

# --- test 2: idempotent re-run ---
"$SCRIPT" --scope global >/dev/null
size1=$(wc -c < "$HOME/.config/opencode/opencode.json")
"$SCRIPT" --scope global >/dev/null
size2=$(wc -c < "$HOME/.config/opencode/opencode.json")
[ "$size1" = "$size2" ] \
  || { echo "test2 FAIL: config size changed ($size1 -> $size2)"; exit 1; }
echo "test2 pass: re-run is idempotent"

# --- test 3: preserve existing model while still merging permissions ---
mk_sandbox preserve-model
cat > "$HOME/.config/opencode/opencode.json" <<'EOF'
{
  "model": "litellm/custom-model"
}
EOF
"$SCRIPT" --scope global >/dev/null
grep -q 'litellm/custom-model' "$HOME/.config/opencode/opencode.json" \
  || { echo "test3 FAIL: existing model overwritten"; cat "$HOME/.config/opencode/opencode.json"; exit 1; }
grep -q 'context-mode' "$HOME/.config/opencode/opencode.json" \
  || { echo "test3 FAIL: MCP not merged"; cat "$HOME/.config/opencode/opencode.json"; exit 1; }
echo "test3 pass: existing model preserved"

# --- test 4: --check after register ---
"$SCRIPT" --check --scope global >/dev/null 2>&1 \
  || { echo "test4 FAIL: --check should pass after register"; exit 1; }
echo "test4 pass: --check passes after register"

# --- test 5: missing npx is a clean error ---
mk_sandbox no-npx
rm "$SANDBOX/bin/npx"
out=$(PATH="$SANDBOX/bin" "$SCRIPT" --scope global 2>&1) && rc=0 || rc=$?
[ "$rc" != "0" ] \
  || { echo "test5 FAIL: expected non-zero when npx missing, got $rc; out=$out"; exit 1; }
echo "$out" | grep -qi 'npx' \
  || { echo "test5 FAIL: expected npx error; got: $out"; exit 1; }
[ ! -f "$HOME/.config/opencode/opencode.json" ] \
  || { echo "test5 FAIL: config written despite missing npx"; cat "$HOME/.config/opencode/opencode.json"; exit 1; }
echo "test5 pass: missing npx is a clean error"

# --- test 6: registration ignores unrelated legacy LiteLLM environment ---
mk_sandbox litellm-env
export LITELLM_API_KEY=test-key
export LITELLM_BASE_URL=http://127.0.0.1:4000/v1
export LITELLM_OPENCODE_MODEL=litellm/frontier-coder
"$SCRIPT" --scope global >/dev/null
if grep -qE 'opencode-plugin-litellm|litellm/frontier-coder' "$HOME/.config/opencode/opencode.json"; then
  echo "test6 FAIL: context-mode registration merged unrelated LiteLLM settings"
  cat "$HOME/.config/opencode/opencode.json"
  exit 1
fi
echo "test6 pass: unrelated LiteLLM environment ignored"

echo
echo "all register-context-mode-opencode tests passed"
