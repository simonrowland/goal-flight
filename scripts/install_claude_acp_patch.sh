#!/usr/bin/env bash
# Opt-in stopgap installer for claude-code-cli-acp@0.1.1 on Claude Code 2.1.169.
#
# The script vendors a fixed upstream patch into a throwaway checkout, builds the
# platform binary, and swaps only the installed npm platform package binary. It
# never runs from install.sh and never mutates anything unless invoked directly.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PATCH_FILE="$REPO_ROOT/patches/claude-code-cli-acp-2.1.169-tui-submit.patch"
UPSTREAM_REPO="https://github.com/moabualruz/claude-code-cli-acp"
BASE_COMMIT="c93f4f4"
STOPGAP_MAX_VERSION="0.1.1"
ALLOW_BIN_OVERRIDE="GOALFLIGHT_ALLOW_CLAUDE_ACP_BIN_OVERRIDE"
ALLOW_PREBUILT_OVERRIDE="GOALFLIGHT_ALLOW_CLAUDE_ACP_PREBUILT_BINARY_OVERRIDE"
ALLOW_VERSION_OVERRIDE="GOALFLIGHT_ALLOW_CLAUDE_ACP_VERSION_OVERRIDE"
CLI_VERSION=""
CLI_BIN_PATH=""
CLI_PREBUILT_BINARY=""

log() {
  printf '%s\n' "$*"
}

fail() {
  local rc="${2:-1}"
  printf 'ERROR: %s\n' "$1" >&2
  exit "$rc"
}

sha256_file() {
  local file="$1"
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | awk '{print $1}'
  elif command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha256 "$file" | awk '{print $NF}'
  else
    fail "no sha256 tool found (need shasum, sha256sum, or openssl)" 2
  fi
}

env_override_warning() {
  printf 'GOALFLIGHT_ENV_OVERRIDE env=%q action=%q reason=%q source=%q\n' "$1" "$2" "$3" "$4" >&2
}

usage() {
  cat <<'USAGE' >&2
usage: install_claude_acp_patch.sh [--version VERSION] [--bin-path PATH] [--prebuilt-binary PATH]
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --version)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      CLI_VERSION="$2"
      shift 2
      ;;
    --bin-path)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      CLI_BIN_PATH="$2"
      shift 2
      ;;
    --prebuilt-binary)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      CLI_PREBUILT_BINARY="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      usage
      fail "unknown argument: $1" 2
      ;;
  esac
done

semver_core() {
  printf '%s' "$1" | sed -E 's/^[^0-9]*//; s/[^0-9.].*$//'
}

num_or_zero() {
  case "${1:-}" in
    ''|*[!0-9]*) printf '0' ;;
    *) printf '%s' "$1" ;;
  esac
}

version_gt() {
  local left right
  left="$(semver_core "$1")"
  right="$(semver_core "$2")"
  local l1 l2 l3 r1 r2 r3
  IFS=. read -r l1 l2 l3 _ <<< "$left"
  IFS=. read -r r1 r2 r3 _ <<< "$right"
  l1="$(num_or_zero "$l1")"; l2="$(num_or_zero "$l2")"; l3="$(num_or_zero "$l3")"
  r1="$(num_or_zero "$r1")"; r2="$(num_or_zero "$r2")"; r3="$(num_or_zero "$r3")"
  if [ "$((10#$l1))" -gt "$((10#$r1))" ]; then return 0; fi
  if [ "$((10#$l1))" -lt "$((10#$r1))" ]; then return 1; fi
  if [ "$((10#$l2))" -gt "$((10#$r2))" ]; then return 0; fi
  if [ "$((10#$l2))" -lt "$((10#$r2))" ]; then return 1; fi
  [ "$((10#$l3))" -gt "$((10#$r3))" ]
}

installed_version() {
  if [ -n "$CLI_VERSION" ]; then
    printf '%s\n' "$CLI_VERSION"
    return 0
  fi
  if [ -n "${GOALFLIGHT_CLAUDE_ACP_VERSION:-}" ]; then
    if [ "${!ALLOW_VERSION_OVERRIDE:-}" = "1" ]; then
      env_override_warning "GOALFLIGHT_CLAUDE_ACP_VERSION" "active" "${ALLOW_VERSION_OVERRIDE}=1" "$GOALFLIGHT_CLAUDE_ACP_VERSION"
      printf '%s\n' "$GOALFLIGHT_CLAUDE_ACP_VERSION"
      return 0
    fi
    env_override_warning "GOALFLIGHT_CLAUDE_ACP_VERSION" "ignored" "${ALLOW_VERSION_OVERRIDE}_not_1" "$GOALFLIGHT_CLAUDE_ACP_VERSION"
  fi
  command -v npm >/dev/null 2>&1 || return 1
  command -v node >/dev/null 2>&1 || return 1
  local npm_root package_json
  npm_root="$(npm root -g 2>/dev/null)" || return 1
  package_json="$npm_root/claude-code-cli-acp/package.json"
  [ -f "$package_json" ] || return 1
  node -e '
    const fs = require("fs");
    const pkg = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
    if (!pkg.version) process.exit(1);
    console.log(pkg.version);
  ' "$package_json"
}

resolve_platform_binary() {
  if [ -n "$CLI_BIN_PATH" ]; then
    printf '%s\n' "$CLI_BIN_PATH"
    return 0
  fi
  if [ -n "${GOALFLIGHT_CLAUDE_ACP_BIN_PATH:-}" ]; then
    if [ "${!ALLOW_BIN_OVERRIDE:-}" = "1" ]; then
      env_override_warning "GOALFLIGHT_CLAUDE_ACP_BIN_PATH" "active" "${ALLOW_BIN_OVERRIDE}=1" "$GOALFLIGHT_CLAUDE_ACP_BIN_PATH"
      printf '%s\n' "$GOALFLIGHT_CLAUDE_ACP_BIN_PATH"
      return 0
    fi
    env_override_warning "GOALFLIGHT_CLAUDE_ACP_BIN_PATH" "ignored" "${ALLOW_BIN_OVERRIDE}_not_1" "$GOALFLIGHT_CLAUDE_ACP_BIN_PATH"
  fi
  command -v npm >/dev/null 2>&1 || return 1
  command -v node >/dev/null 2>&1 || return 1
  local npm_root resolver_dir resolved rc
  npm_root="$(npm root -g 2>/dev/null)" || return 1
  resolver_dir="$(mktemp -d)"
  ln -s "$npm_root" "$resolver_dir/node_modules"
  set +e
  resolved="$(
    cd "$resolver_dir" && node --input-type=module <<'NODE'
import { fileURLToPath } from "url";
const platform = process.platform;
const archMap = { x64: "x64", arm64: "arm64" };
const arch = archMap[process.arch];
if (!["darwin", "linux", "win32"].includes(platform) || !arch) {
  throw new Error(`unsupported platform ${platform}-${process.arch}`);
}
const exe = platform === "win32" ? "claude-code-cli-acp.exe" : "claude-code-cli-acp";
const pkg = `claude-code-cli-acp-${platform}-${arch}`;
const url = await import.meta.resolve(`${pkg}/bin/${exe}`);
console.log(fileURLToPath(url));
NODE
  )"
  rc=$?
  set -e
  rm -rf "$resolver_dir"
  [ "$rc" -eq 0 ] || return "$rc"
  printf '%s\n' "$resolved"
}

if [ ! -f "$PATCH_FILE" ]; then
  fail "vendored patch missing: $PATCH_FILE" 2
fi

VERSION="$(installed_version || true)"
if [ -z "$VERSION" ]; then
  fail "claude-code-cli-acp npm package not found; install it first, then re-run this opt-in stopgap" 2
fi

if version_gt "$VERSION" "$STOPGAP_MAX_VERSION"; then
  log "SKIP: installed claude-code-cli-acp version $VERSION is newer than $STOPGAP_MAX_VERSION; upstream should include the fix."
  exit 0
fi

BIN_PATH="$(resolve_platform_binary || true)"
if [ -z "$BIN_PATH" ] || [ ! -f "$BIN_PATH" ]; then
  fail "could not resolve installed platform binary for claude-code-cli-acp; reinstall the npm package, then re-run" 2
fi

WORK_DIR="$(mktemp -d)"
RUN_BACKUP="$WORK_DIR/installed-pre-run"
cleanup() {
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

if ! cp -p "$BIN_PATH" "$RUN_BACKUP"; then
  fail "could not create run-scoped backup for $BIN_PATH; refusing to patch" 2
fi

restore_run_backup() {
  local reason="$1"
  local rc="${2:-2}"
  if [ -n "${RUN_BACKUP:-}" ] && [ -f "$RUN_BACKUP" ] && [ -n "${BIN_PATH:-}" ]; then
    if cp -p "$RUN_BACKUP" "$BIN_PATH"; then
      fail "$reason; restored $BIN_PATH from run-scoped backup $RUN_BACKUP" "$rc"
    fi
    fail "$reason; restore from run-scoped backup failed for $BIN_PATH (backup: $RUN_BACKUP)" "$rc"
  fi
  fail "$reason; no run-scoped backup available for $BIN_PATH" "$rc"
}

PREBUILT_BINARY="$CLI_PREBUILT_BINARY"
if [ -z "$PREBUILT_BINARY" ] && [ -n "${GOALFLIGHT_CLAUDE_ACP_PREBUILT_BINARY:-}" ]; then
  if [ "${!ALLOW_PREBUILT_OVERRIDE:-}" = "1" ]; then
    PREBUILT_BINARY="$GOALFLIGHT_CLAUDE_ACP_PREBUILT_BINARY"
    env_override_warning "GOALFLIGHT_CLAUDE_ACP_PREBUILT_BINARY" "active" "${ALLOW_PREBUILT_OVERRIDE}=1" "$PREBUILT_BINARY"
  else
    env_override_warning "GOALFLIGHT_CLAUDE_ACP_PREBUILT_BINARY" "ignored" "${ALLOW_PREBUILT_OVERRIDE}_not_1" "$GOALFLIGHT_CLAUDE_ACP_PREBUILT_BINARY"
  fi
fi

if [ -z "$PREBUILT_BINARY" ] && {
  [ -n "${GOALFLIGHT_CLAUDE_ACP_FORCE_CARGO_MISSING:-}" ] || ! command -v cargo >/dev/null 2>&1
}; then
  log "SKIP: Rust cargo is required to build the stopgap binary."
  log "Install Rust from https://rustup.rs/ or your package manager, then re-run scripts/install_claude_acp_patch.sh."
  exit 3
fi

if [ -n "$PREBUILT_BINARY" ]; then
  BUILT_BINARY="$PREBUILT_BINARY"
  [ -f "$BUILT_BINARY" ] || fail "prebuilt test binary missing: $BUILT_BINARY" 2
  log "prebuilt binary: $BUILT_BINARY sha256=$(sha256_file "$BUILT_BINARY")"
else
  log "Cloning $UPSTREAM_REPO at $BASE_COMMIT..."
  git clone --quiet "$UPSTREAM_REPO" "$WORK_DIR/claude-code-cli-acp"
  git -C "$WORK_DIR/claude-code-cli-acp" checkout --quiet "$BASE_COMMIT"
  git -C "$WORK_DIR/claude-code-cli-acp" apply "$PATCH_FILE"
  log "Building patched claude-code-cli-acp release binary..."
  cargo build --release --manifest-path "$WORK_DIR/claude-code-cli-acp/Cargo.toml"
  EXE_NAME="claude-code-cli-acp"
  case "$(uname -s)" in
    MINGW*|MSYS*) EXE_NAME="claude-code-cli-acp.exe" ;;
  esac
  BUILT_BINARY="$WORK_DIR/claude-code-cli-acp/target/release/$EXE_NAME"
fi

[ -f "$BUILT_BINARY" ] || fail "build did not produce $BUILT_BINARY" 2

BEFORE_SHA="$(sha256_file "$BIN_PATH")"
PATCHED_SHA="$(sha256_file "$BUILT_BINARY")"
log "installed binary: $BIN_PATH"
log "before sha256:   $BEFORE_SHA"
log "patched sha256:  $PATCHED_SHA"

if [ "$BEFORE_SHA" = "$PATCHED_SHA" ]; then
  log "SKIP: installed claude-code-cli-acp binary already matches the patched build."
  if [ -f "$BIN_PATH.orig" ]; then
    log "revert: cp \"$BIN_PATH.orig\" \"$BIN_PATH\" && chmod 755 \"$BIN_PATH\""
  fi
  exit 0
fi

if [ ! -f "$BIN_PATH.orig" ]; then
  cp -p "$RUN_BACKUP" "$BIN_PATH.orig"
  log "backup: $BIN_PATH.orig"
else
  log "backup: $BIN_PATH.orig already exists; leaving it untouched"
fi

cp "$BUILT_BINARY" "$BIN_PATH" || restore_run_backup "binary swap failed"
chmod 755 "$BIN_PATH" || restore_run_backup "chmod failed after binary swap"

if [ "$(uname -s)" = "Darwin" ]; then
  command -v xattr >/dev/null 2>&1 || restore_run_backup "xattr missing on macOS after binary swap"
  command -v codesign >/dev/null 2>&1 || restore_run_backup "codesign missing on macOS after binary swap"
  xattr -c "$BIN_PATH" || restore_run_backup "xattr failed after binary swap"
  codesign -s - --force "$BIN_PATH" || restore_run_backup "codesign failed after binary swap"
fi

AFTER_SHA="$(sha256_file "$BIN_PATH")"
log "after sha256:    $AFTER_SHA"
log "OK: installed patched claude-code-cli-acp stopgap."
log "revert: cp \"$BIN_PATH.orig\" \"$BIN_PATH\" && chmod 755 \"$BIN_PATH\""
