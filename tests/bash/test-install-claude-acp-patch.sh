#!/usr/bin/env bash
# Branch tests for scripts/install_claude_acp_patch.sh compatibility entrypoint.

set -eu

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPT="$ROOT/scripts/install_claude_acp_patch.sh"
[ -x "$SCRIPT" ] || { echo "script not executable: $SCRIPT"; exit 1; }

grep -q 'PINNED_FIX_COMMIT="14a5b0c"' "$SCRIPT" \
  || { echo "static-pin FAIL: build script does not pin 14a5b0c"; exit 1; }
stale_var="PATCH""_FILE"
! grep -q "$stale_var" "$SCRIPT" \
  || { echo "static-pin FAIL: build script still references old patch variable"; exit 1; }
! grep -Eq 'git .* apply' "$SCRIPT" \
  || { echo "static-pin FAIL: build script still applies a local patch"; exit 1; }
! find "$ROOT/patches" -name '*.patch' -print -quit | grep -q . \
  || { echo "static-pin FAIL: superseded vendored patch still exists"; exit 1; }
grep -q 'scripts/hosts/fleet/install_claude_acp.sh' "$ROOT/install.sh" \
  || { echo "default-install FAIL: install.sh claude-acp route missing"; exit 1; }
grep -q 'scripts/install_claude_acp_patch.sh' "$ROOT/scripts/hosts/fleet/install_claude_acp.sh" \
  || { echo "default-install FAIL: claude-acp installer does not run pinned build"; exit 1; }
echo "static-pin/default-install pass"

TMPROOT="$(mktemp -d /tmp/goal-flight-claude-acp-patch-test-XXXXXX)"
trap 'rm -rf "$TMPROOT"' EXIT

target_dir="$TMPROOT/target bin"
patched_dir="$TMPROOT/patched build"
mkdir -p "$target_dir" "$patched_dir"
target="$target_dir/claude-code-cli-acp"
patched="$patched_dir/patched-claude-code-cli-acp"
printf 'original\n' > "$target"
printf 'patched\n' > "$patched"
chmod 755 "$target" "$patched"

warning_field_equals() {
  WARNING_TEXT="$4" python3 - "$1" "$2" "$3" <<'PY'
import os
import shlex
import sys

env_name, field, expected = sys.argv[1:4]
for line in os.environ["WARNING_TEXT"].splitlines():
    if "GOALFLIGHT_ENV_OVERRIDE" not in line:
        continue
    tokens = shlex.split(line)
    fields = dict(token.split("=", 1) for token in tokens[1:] if "=" in token)
    if fields.get("env") == env_name and fields.get(field) == expected:
        raise SystemExit(0)
raise SystemExit(1)
PY
}

out=$(
  GOALFLIGHT_CLAUDE_ACP_VERSION=9.9.9 \
  GOALFLIGHT_CLAUDE_ACP_FORCE_CARGO_MISSING=1 \
  "$SCRIPT" 2>&1
) && rc=0 || rc=$?
echo "$out" | grep -q "env=GOALFLIGHT_CLAUDE_ACP_VERSION action=ignored" \
  || { echo "version-ungated FAIL: expected ignored warning; out=$out"; exit 1; }
echo "version-ungated pass"

out=$(
  GOALFLIGHT_CLAUDE_ACP_BIN_PATH="$target" \
  "$SCRIPT" --version 0.1.1 2>&1
) && rc=0 || rc=$?
echo "$out" | grep -q "env=GOALFLIGHT_CLAUDE_ACP_BIN_PATH action=ignored" \
  || { echo "bin-ungated FAIL: expected ignored warning; out=$out"; exit 1; }
echo "bin-ungated pass"

out=$(
  GOALFLIGHT_CLAUDE_ACP_PREBUILT_BINARY="$patched" \
  GOALFLIGHT_CLAUDE_ACP_FORCE_CARGO_MISSING=1 \
  "$SCRIPT" --version 0.1.1 --bin-path "$target" 2>&1
) && rc=0 || rc=$?
echo "$out" | grep -q "env=GOALFLIGHT_CLAUDE_ACP_PREBUILT_BINARY action=ignored" \
  || { echo "prebuilt-ungated FAIL: expected ignored warning; out=$out"; exit 1; }
warning_field_equals "GOALFLIGHT_CLAUDE_ACP_PREBUILT_BINARY" "source" "$patched" "$out" \
  || { echo "prebuilt-ungated FAIL: source did not round-trip; out=$out"; exit 1; }
! echo "$out" | grep -q "prebuilt binary:" \
  || { echo "prebuilt-ungated FAIL: ungated prebuilt was honored; out=$out"; exit 1; }
echo "prebuilt-ungated pass"

out=$(
  GOALFLIGHT_CLAUDE_ACP_VERSION=0.1.2 \
  GOALFLIGHT_ALLOW_CLAUDE_ACP_VERSION_OVERRIDE=1 \
  GOALFLIGHT_CLAUDE_ACP_FORCE_CARGO_MISSING=1 \
  "$SCRIPT" 2>&1
) && rc=0 || rc=$?
[ "$rc" = "0" ] || { echo "newer-version FAIL: rc=$rc out=$out"; exit 1; }
echo "$out" | grep -q "newer than 0.1.1" \
  || { echo "newer-version FAIL: expected pinned-build skip; out=$out"; exit 1; }
echo "newer-version pass"

out=$(
  GOALFLIGHT_CLAUDE_ACP_VERSION=0.1.1 \
  GOALFLIGHT_ALLOW_CLAUDE_ACP_VERSION_OVERRIDE=1 \
  GOALFLIGHT_CLAUDE_ACP_FORCE_CARGO_MISSING=1 \
  GOALFLIGHT_SKIP_CLAUDE_ACP_PINNED_BUILD=1 \
  "$SCRIPT" 2>&1
) && rc=0 || rc=$?
[ "$rc" = "0" ] || { echo "opt-out FAIL: rc=$rc out=$out"; exit 1; }
echo "$out" | grep -q "GOALFLIGHT_SKIP_CLAUDE_ACP_PINNED_BUILD=1" \
  || { echo "opt-out FAIL: expected explicit opt-out warning; out=$out"; exit 1; }
echo "$out" | grep -q "known broken" \
  || { echo "opt-out FAIL: expected broken-shim warning; out=$out"; exit 1; }
echo "opt-out pass"

out=$(
  GOALFLIGHT_CLAUDE_ACP_VERSION=0.1.1 \
  GOALFLIGHT_ALLOW_CLAUDE_ACP_VERSION_OVERRIDE=1 \
  GOALFLIGHT_CLAUDE_ACP_BIN_PATH="$target" \
  GOALFLIGHT_ALLOW_CLAUDE_ACP_BIN_OVERRIDE=1 \
  GOALFLIGHT_CLAUDE_ACP_FORCE_CARGO_MISSING=1 \
  "$SCRIPT" 2>&1
) && rc=0 || rc=$?
[ "$rc" = "3" ] || { echo "cargo-absent FAIL: expected rc=3 got $rc out=$out"; exit 1; }
echo "$out" | grep -q "Rust cargo is required" \
  || { echo "cargo-absent FAIL: expected cargo message; out=$out"; exit 1; }
echo "$out" | grep -q "GOALFLIGHT_SKIP_CLAUDE_ACP_PINNED_BUILD=1" \
  || { echo "cargo-absent FAIL: expected opt-out hint; out=$out"; exit 1; }
echo "cargo-absent pass"

layout_fakebin="$TMPROOT/layout-fakebin"
mkdir "$layout_fakebin"
cat > "$layout_fakebin/npm" <<'SH'
#!/usr/bin/env sh
if [ "$1" = "root" ] && [ "$2" = "-g" ]; then
  printf '%s\n' "$FAKE_NPM_ROOT"
  exit 0
fi
exit 1
SH
cat > "$layout_fakebin/uname" <<'SH'
#!/usr/bin/env sh
case "${1:-}" in
  -s|'') printf 'Linux\n' ;;
  -m) printf 'x86_64\n' ;;
  *) exit 1 ;;
esac
SH
chmod 755 "$layout_fakebin/npm" "$layout_fakebin/uname"

platform_pkg="claude-code-cli-acp-linux-x64"
run_layout_resolution_case() {
  case_name="$1"
  placement="$2"
  npm_root="$TMPROOT/$case_name/npm-root"
  launcher_dir="$npm_root/claude-code-cli-acp"
  mkdir -p "$launcher_dir"
  expected_bin=""
  if [ "$placement" = "nested" ]; then
    expected_bin="$launcher_dir/node_modules/$platform_pkg/bin/claude-code-cli-acp"
  elif [ "$placement" = "top-level" ]; then
    expected_bin="$npm_root/$platform_pkg/bin/claude-code-cli-acp"
  fi
  if [ -n "$expected_bin" ]; then
    mkdir -p "$(dirname "$expected_bin")"
    printf 'original-%s\n' "$case_name" > "$expected_bin"
    chmod 755 "$expected_bin"
  fi

  out=$(
    PATH="$layout_fakebin:$PATH" \
    FAKE_NPM_ROOT="$npm_root" \
    "$SCRIPT" --version 0.1.1 --prebuilt-binary "$patched" 2>&1
  ) && rc=0 || rc=$?

  if [ "$placement" = "absent" ]; then
    [ "$rc" = "2" ] || { echo "$case_name FAIL: expected rc=2 got $rc out=$out"; exit 1; }
    echo "$out" | grep -Fq "could not locate the installed claude-code-cli-acp platform binary under:" \
      || { echo "$case_name FAIL: expected locate failure message; out=$out"; exit 1; }
    echo "$out" | grep -Fq "$npm_root/$platform_pkg/bin/claude-code-cli-acp" \
      || { echo "$case_name FAIL: expected top-level path in message; out=$out"; exit 1; }
    echo "$out" | grep -Fq "$launcher_dir/node_modules/$platform_pkg/bin/claude-code-cli-acp" \
      || { echo "$case_name FAIL: expected nested path in message; out=$out"; exit 1; }
    echo "$out" | grep -Fq "reinstall the npm package and re-run" \
      || { echo "$case_name FAIL: expected reinstall hint; out=$out"; exit 1; }
    echo "$case_name pass"
    return 0
  fi

  [ "$rc" = "0" ] || { echo "$case_name FAIL: rc=$rc out=$out"; exit 1; }
  echo "$out" | grep -Fq "installed binary: $expected_bin" \
    || { echo "$case_name FAIL: expected resolved path $expected_bin; out=$out"; exit 1; }
  [ "$(cat "$expected_bin")" = "patched" ] \
    || { echo "$case_name FAIL: target was not swapped; target=$(cat "$expected_bin") out=$out"; exit 1; }
  [ "$(cat "$expected_bin.orig")" = "original-$case_name" ] \
    || { echo "$case_name FAIL: persistent .orig was not created from original; orig=$(cat "$expected_bin.orig") out=$out"; exit 1; }
  echo "$case_name pass"
}

run_layout_resolution_case "nested-layout" "nested"
run_layout_resolution_case "top-level-layout" "top-level"
run_layout_resolution_case "missing-layout" "absent"

cp "$patched" "$target"
out=$(
  GOALFLIGHT_CLAUDE_ACP_VERSION=0.1.1 \
  GOALFLIGHT_ALLOW_CLAUDE_ACP_VERSION_OVERRIDE=1 \
  GOALFLIGHT_CLAUDE_ACP_BIN_PATH="$target" \
  GOALFLIGHT_ALLOW_CLAUDE_ACP_BIN_OVERRIDE=1 \
  GOALFLIGHT_CLAUDE_ACP_PREBUILT_BINARY="$patched" \
  GOALFLIGHT_ALLOW_CLAUDE_ACP_PREBUILT_BINARY_OVERRIDE=1 \
  "$SCRIPT" 2>&1
) && rc=0 || rc=$?
[ "$rc" = "0" ] || { echo "already-patched FAIL: rc=$rc out=$out"; exit 1; }
echo "$out" | grep -q "env=GOALFLIGHT_CLAUDE_ACP_PREBUILT_BINARY action=active" \
  || { echo "already-patched FAIL: expected active prebuilt warning; out=$out"; exit 1; }
warning_field_equals "GOALFLIGHT_CLAUDE_ACP_PREBUILT_BINARY" "source" "$patched" "$out" \
  || { echo "already-patched FAIL: source did not round-trip; out=$out"; exit 1; }
echo "$out" | grep -Eq "prebuilt binary: .* sha256=[0-9a-f]{64}" \
  || { echo "already-patched FAIL: expected prebuilt sha256; out=$out"; exit 1; }
echo "$out" | grep -q "already matches the pinned build" \
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
    GOALFLIGHT_ALLOW_CLAUDE_ACP_VERSION_OVERRIDE=1 \
    GOALFLIGHT_CLAUDE_ACP_BIN_PATH="$target" \
    GOALFLIGHT_ALLOW_CLAUDE_ACP_BIN_OVERRIDE=1 \
    GOALFLIGHT_CLAUDE_ACP_PREBUILT_BINARY="$patched" \
    GOALFLIGHT_ALLOW_CLAUDE_ACP_PREBUILT_BINARY_OVERRIDE=1 \
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
