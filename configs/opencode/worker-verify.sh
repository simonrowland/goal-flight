#!/usr/bin/env sh
# Verify OpenCode worker surface (matches adapters/opencode.json discovery probes).
set -eu

export PATH="${HOME}/.opencode/bin:${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

bin=""
if command -v opencode >/dev/null 2>&1; then
  bin=$(command -v opencode)
elif [ -x "${HOME}/.local/bin/opencode" ]; then
  bin="${HOME}/.local/bin/opencode"
elif [ -x "${HOME}/.opencode/bin/opencode" ]; then
  bin="${HOME}/.opencode/bin/opencode"
fi

if [ -z "$bin" ]; then
  echo "opencode: not found (checked PATH, ~/.local/bin, ~/.opencode/bin)" >&2
  exit 1
fi

echo "opencode binary: $bin"
"$bin" --version

if "$bin" acp --help >/dev/null 2>&1; then
  echo "opencode acp: help available"
elif "$bin" --help 2>&1 | grep -qi acp; then
  echo "opencode acp: listed in main help"
else
  echo "opencode acp: not available" >&2
  exit 1
fi

echo "worker verify: ok"
