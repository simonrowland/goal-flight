#!/usr/bin/env bash
# Compatibility entrypoint for installing a working claude-code-cli-acp shim.
#
# The npm package still lays down the launcher and per-platform package. For
# claude-code-cli-acp <= 0.1.1, this script builds the upstream merged fix commit
# from source and swaps only the installed npm platform package binary.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UPSTREAM_REPO="https://github.com/moabualruz/claude-code-cli-acp"
PINNED_FIX_COMMIT="14a5b0c"
STOPGAP_MAX_VERSION="0.1.1"
SKIP_PINNED_BUILD_ENV="GOALFLIGHT_SKIP_CLAUDE_ACP_PINNED_BUILD"
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

Builds claude-code-cli-acp from pinned upstream commit 14a5b0c when the installed
npm package is <= 0.1.1. The script name is kept for compatibility.
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

missing_cargo() {
  cat >&2 <<EOF
ERROR: Rust cargo is required to build the working claude-code-cli-acp shim.
Goal Flight must build upstream commit ${PINNED_FIX_COMMIT} until npm publishes claude-code-cli-acp > ${STOPGAP_MAX_VERSION}.
Install Rust from https://rustup.rs/ or your package manager, then re-run ./install.sh claude-acp.
Temporary opt-out, accepting the broken npm ${STOPGAP_MAX_VERSION} shim: ${SKIP_PINNED_BUILD_ENV}=1 ./install.sh claude-acp
EOF
  exit 3
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
  local npm_root platform arch platform_arch exe pkg launcher_dir candidate resolved rc
  npm_root="$(npm root -g 2>/dev/null)" || return 1
  case "$(uname -s)" in
    Darwin) platform="darwin" ;;
    Linux) platform="linux" ;;
    MINGW*|MSYS*|CYGWIN*) platform="win32" ;;
    *) return 1 ;;
  esac
  case "$(uname -m)" in
    arm64|aarch64) arch="arm64" ;;
    x86_64|amd64) arch="x64" ;;
    *) return 1 ;;
  esac
  platform_arch="${platform}-${arch}"
  exe="claude-code-cli-acp"
  [ "$platform" = "win32" ] && exe="claude-code-cli-acp.exe"
  pkg="claude-code-cli-acp-${platform_arch}"
  launcher_dir="$npm_root/claude-code-cli-acp"
  for candidate in \
    "$npm_root/$pkg/bin/$exe" \
    "$launcher_dir/node_modules/$pkg/bin/$exe"
  do
    if [ -f "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  set +e
  resolved="$(
    node - "$launcher_dir" "$pkg" "$exe" <<'NODE'
const [launcherDir, pkg, exe] = process.argv.slice(2);
try {
  console.log(require.resolve(`${pkg}/bin/${exe}`, { paths: [launcherDir] }));
} catch {
  process.exit(1);
}
NODE
  )"
  rc=$?
  set -e
  [ "$rc" -eq 0 ] && [ -f "$resolved" ] || return 1
  printf '%s\n' "$resolved"
}

platform_binary_search_paths() {
  command -v npm >/dev/null 2>&1 || return 1
  local npm_root platform arch platform_arch exe pkg launcher_dir
  npm_root="$(npm root -g 2>/dev/null)" || return 1
  case "$(uname -s)" in
    Darwin) platform="darwin" ;;
    Linux) platform="linux" ;;
    MINGW*|MSYS*|CYGWIN*) platform="win32" ;;
    *) return 1 ;;
  esac
  case "$(uname -m)" in
    arm64|aarch64) arch="arm64" ;;
    x86_64|amd64) arch="x64" ;;
    *) return 1 ;;
  esac
  platform_arch="${platform}-${arch}"
  exe="claude-code-cli-acp"
  [ "$platform" = "win32" ] && exe="claude-code-cli-acp.exe"
  pkg="claude-code-cli-acp-${platform_arch}"
  launcher_dir="$npm_root/claude-code-cli-acp"
  printf '%s\n' \
    "$npm_root/$pkg/bin/$exe" \
    "$launcher_dir/node_modules/$pkg/bin/$exe"
}

VERSION="$(installed_version || true)"
if [ -z "$VERSION" ]; then
  fail "claude-code-cli-acp npm package not found; run ./install.sh claude-acp so npm installs the launcher first" 2
fi

if version_gt "$VERSION" "$STOPGAP_MAX_VERSION"; then
  log "SKIP: installed claude-code-cli-acp version $VERSION is newer than $STOPGAP_MAX_VERSION; npm release should include the fix."
  exit 0
fi

if [ "${!SKIP_PINNED_BUILD_ENV:-}" = "1" ]; then
  log "WARN: ${SKIP_PINNED_BUILD_ENV}=1; leaving claude-code-cli-acp $VERSION npm binary in place."
  log "WARN: versions <= $STOPGAP_MAX_VERSION are known broken for Claude Code 2.1.169 TUI submit; unset the env var and install Rust cargo to build $PINNED_FIX_COMMIT."
  exit 0
fi

if ! BIN_PATH="$(resolve_platform_binary)"; then
  SEARCH_PATHS="$(platform_binary_search_paths 2>/dev/null | paste -sd ', ' - || true)"
  [ -n "$SEARCH_PATHS" ] || SEARCH_PATHS="npm root -g unavailable or unsupported platform"
  fail "could not locate the installed claude-code-cli-acp platform binary under: $SEARCH_PATHS; reinstall the npm package and re-run" 2
fi
if [ ! -f "$BIN_PATH" ]; then
  fail "resolved installed claude-code-cli-acp platform binary is not a file: $BIN_PATH; reinstall the npm package and re-run" 2
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
  missing_cargo
fi

if [ -z "$PREBUILT_BINARY" ] && ! command -v git >/dev/null 2>&1; then
  fail "git is required to clone $UPSTREAM_REPO at pinned fix $PINNED_FIX_COMMIT" 3
fi

if [ -n "$PREBUILT_BINARY" ]; then
  BUILT_BINARY="$PREBUILT_BINARY"
  [ -f "$BUILT_BINARY" ] || fail "prebuilt test binary missing: $BUILT_BINARY" 2
  log "prebuilt binary: $BUILT_BINARY sha256=$(sha256_file "$BUILT_BINARY")"
else
  log "Cloning $UPSTREAM_REPO at merged fix $PINNED_FIX_COMMIT..."
  git clone --quiet "$UPSTREAM_REPO" "$WORK_DIR/claude-code-cli-acp"
  git -C "$WORK_DIR/claude-code-cli-acp" checkout --quiet "$PINNED_FIX_COMMIT"
  log "Building pinned claude-code-cli-acp release binary..."
  cargo build --release --manifest-path "$WORK_DIR/claude-code-cli-acp/Cargo.toml"
  EXE_NAME="claude-code-cli-acp"
  case "$(uname -s)" in
    MINGW*|MSYS*) EXE_NAME="claude-code-cli-acp.exe" ;;
  esac
  BUILT_BINARY="$WORK_DIR/claude-code-cli-acp/target/release/$EXE_NAME"
fi

[ -f "$BUILT_BINARY" ] || fail "build did not produce $BUILT_BINARY" 2

BEFORE_SHA="$(sha256_file "$BIN_PATH")"
BUILT_SHA="$(sha256_file "$BUILT_BINARY")"
log "installed binary: $BIN_PATH"
log "before sha256:   $BEFORE_SHA"
log "built sha256:    $BUILT_SHA"

if [ "$BEFORE_SHA" = "$BUILT_SHA" ]; then
  log "SKIP: installed claude-code-cli-acp binary already matches the pinned build."
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
log "OK: installed claude-code-cli-acp pinned build $PINNED_FIX_COMMIT."
log "revert: cp \"$BIN_PATH.orig\" \"$BIN_PATH\" && chmod 755 \"$BIN_PATH\""
