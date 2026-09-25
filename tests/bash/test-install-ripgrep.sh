#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL_SH="$REPO_ROOT/install.sh"
TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/gf-install-rg-XXXXXX")"
trap 'rm -rf "$TMPROOT"' EXIT

FAKE_BIN="$TMPROOT/bin"
mkdir -p "$FAKE_BIN"
BREW_LOG="$TMPROOT/brew.log"
export BREW_LOG

source "$INSTALL_SH"

assert_contains() {
  local needle="$1"
  local haystack="$2"
  if [[ "$haystack" != *"$needle"* ]]; then
    printf 'FAIL: expected %s in output: %s\n' "$needle" "$haystack" >&2
    exit 1
  fi
}

printf '#!/bin/sh\nprintf "%%s\\n" "$*" >> "%s"\nexit 1\n' "$BREW_LOG" > "$FAKE_BIN/brew"
chmod +x "$FAKE_BIN/brew"
printf '#!/bin/sh\nprintf "Darwin\\n"\n' > "$FAKE_BIN/uname"
chmod +x "$FAKE_BIN/uname"

NO_DEPS=1
skip_output="$(PATH="$FAKE_BIN" ensure_ripgrep 2>&1)"
assert_contains 'skipped (--no-deps)' "$skip_output"
[[ ! -s "$BREW_LOG" ]] || { echo 'FAIL: --no-deps invoked brew' >&2; exit 1; }
echo 'test1 pass: --no-deps skips package manager'

NO_DEPS=0
brew_output="$(PATH="$FAKE_BIN" ensure_ripgrep </dev/null 2>&1)"
assert_contains 'brew install ripgrep' "$brew_output"
[[ ! -s "$BREW_LOG" ]] || { echo 'FAIL: non-interactive install invoked brew' >&2; exit 1; }
echo 'test2 pass: non-interactive macOS install prints brew command and continues'

: > "$BREW_LOG"
ci_output="$(CI=1 PATH="$FAKE_BIN" ensure_ripgrep 2>&1)"
assert_contains 'brew install ripgrep' "$ci_output"
[[ ! -s "$BREW_LOG" ]] || { echo 'FAIL: CI install invoked brew' >&2; exit 1; }
echo 'test3 pass: CI macOS install prints brew command and continues'

: > "$BREW_LOG"
PYTHON_BIN="$(command -v python3)"
interactive_output="$(PATH="$FAKE_BIN" "$PYTHON_BIN" - "$INSTALL_SH" "$FAKE_BIN" <<'PY'
import os
import pty
import sys

install_sh, fake_bin = sys.argv[1:3]
pid, fd = pty.fork()
if pid == 0:
    child_env = os.environ.copy()
    child_env["PATH"] = fake_bin
    for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "TF_BUILD", "CIRCLECI", "TRAVIS", "JENKINS_URL", "BUILDKITE", "DRONE"):
        child_env.pop(name, None)
    os.execve(
        "/bin/bash",
        ["bash", "-c", 'source "$1"; NO_DEPS=0; ensure_ripgrep', "bash", install_sh],
        child_env,
    )

output = bytearray()
while True:
    try:
        chunk = os.read(fd, 4096)
    except OSError:
        break
    if not chunk:
        break
    output.extend(chunk)
_, status = os.waitpid(pid, 0)
if os.waitstatus_to_exitcode(status) != 0:
    raise SystemExit(1)
sys.stdout.buffer.write(output)
PY
)"
assert_contains 'brew install ripgrep' "$interactive_output"
grep -qx 'install ripgrep' "$BREW_LOG"
echo 'test4 pass: interactive non-CI macOS install invokes brew'

printf '#!/bin/sh\nexit 0\n' > "$FAKE_BIN/rg"
chmod +x "$FAKE_BIN/rg"
present_output="$(PATH="$FAKE_BIN" ensure_ripgrep 2>&1)"
assert_contains "present ($FAKE_BIN/rg)" "$present_output"
echo 'test5 pass: existing rg is idempotent'

printf '#!/bin/sh\nexit 1\n' > "$FAKE_BIN/rg"
chmod +x "$FAKE_BIN/rg"
: > "$BREW_LOG"
broken_output="$(PATH="$FAKE_BIN" ensure_ripgrep </dev/null 2>&1)"
assert_contains 'rg --version failed' "$broken_output"
assert_contains 'brew install ripgrep' "$broken_output"
[[ ! -s "$BREW_LOG" ]] || { echo 'FAIL: non-interactive broken-rg check invoked brew' >&2; exit 1; }
echo 'test6 pass: broken rg is not treated as usable'

rm -f "$FAKE_BIN/rg"
PKG_LOG="$TMPROOT/package-manager.log"
printf '#!/bin/sh\nprintf "invoked\\n" >> "%s"\nexit 99\n' "$PKG_LOG" > "$FAKE_BIN/apt-get"
chmod +x "$FAKE_BIN/apt-get"
printf '#!/bin/sh\nprintf "Linux\\n"\n' > "$FAKE_BIN/uname"
chmod +x "$FAKE_BIN/uname"
linux_output="$(PATH="$FAKE_BIN" ensure_ripgrep 2>&1)"
assert_contains 'sudo apt-get update && sudo apt-get install -y ripgrep' "$linux_output"
[[ ! -s "$PKG_LOG" ]] || { echo 'FAIL: Linux dependency check ran apt-get' >&2; exit 1; }
echo 'test7 pass: Linux path prints apt command without running package manager'

FAKE_SETUP="$TMPROOT/setup.sh"
SETUP_LOG="$TMPROOT/setup-args.log"
printf '#!/bin/sh\nprintf "%%s\\n" "$@" > "%s"\n' "$SETUP_LOG" > "$FAKE_SETUP"
chmod +x "$FAKE_SETUP"
source "$INSTALL_SH" --no-deps --sentinel
SETUP="$FAKE_SETUP"
run_setup_and_traits "${INSTALL_ARGS[@]}" >/dev/null
grep -qx -- '--sentinel' "$SETUP_LOG"
if grep -qx -- '--no-deps' "$SETUP_LOG"; then
  echo 'FAIL: --no-deps leaked into setup argv' >&2
  exit 1
fi
echo 'test8 pass: --no-deps is stripped before setup'
