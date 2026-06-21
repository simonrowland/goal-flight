#!/usr/bin/env bash
# Install the out-of-session launchd drainer for durable dispatch queue work.

set -euo pipefail

LABEL="com.goalflight.drain"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEMPLATE="${REPO_ROOT}/scripts/templates/${LABEL}.plist.tmpl"

DRY_RUN=0
UNINSTALL=0
STATUS=0
SKILL_ROOT="${SKILL_ROOT:-${GOALFLIGHT_SKILL_ROOT:-}}"
LOG_PATH="${GOALFLIGHT_DRAIN_LOG:-}"

usage() {
  cat <<'EOF'
Usage:
  scripts/install-drainer.sh [--skill-root <path>] [--dry-run]
  scripts/install-drainer.sh --status
  scripts/install-drainer.sh --uninstall

Installs com.goalflight.drain as a per-user launchd agent. The agent runs:
  python3 <skill-root>/scripts/goalflight_dispatch.py drain --json

Environment:
  SKILL_ROOT or GOALFLIGHT_SKILL_ROOT  override ~/.goal-flight/skill
  GOALFLIGHT_DRAIN_LOG                override ~/.goal-flight/drain-launchd.log
  GOALFLIGHT_DRAIN_PATH               override rendered launchd PATH
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      ;;
    --uninstall)
      UNINSTALL=1
      ;;
    --status)
      STATUS=1
      ;;
    --skill-root)
      shift
      [ "$#" -gt 0 ] || { echo "ERROR: --skill-root needs a path" >&2; exit 2; }
      SKILL_ROOT="$1"
      ;;
    --skill-root=*)
      SKILL_ROOT="${1#--skill-root=}"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

mode_count=$((DRY_RUN + UNINSTALL + STATUS))
if [ "$mode_count" -gt 1 ]; then
  echo "ERROR: choose only one of --dry-run, --uninstall, or --status" >&2
  exit 2
fi

HOME_DIR="${HOME:-}"
if [ -z "$HOME_DIR" ]; then
  echo "ERROR: HOME is not set" >&2
  exit 2
fi

expand_home() {
  case "$1" in
    "~") printf '%s\n' "$HOME_DIR" ;;
    "~/"*) printf '%s/%s\n' "$HOME_DIR" "${1#~/}" ;;
    *) printf '%s\n' "$1" ;;
  esac
}

if [ -z "$SKILL_ROOT" ]; then
  SKILL_ROOT="${HOME_DIR}/.goal-flight/skill"
fi
SKILL_ROOT="$(expand_home "$SKILL_ROOT")"
LOG_PATH="$(expand_home "${LOG_PATH:-${HOME_DIR}/.goal-flight/drain-launchd.log}")"
PLIST_PATH="${HOME_DIR}/Library/LaunchAgents/${LABEL}.plist"
LAUNCH_DOMAIN="gui/$(id -u)"

PYTHON_BIN="$(command -v python3 || true)"
if [ -z "$PYTHON_BIN" ]; then
  echo "ERROR: python3 not found on PATH" >&2
  exit 2
fi

dedupe_path() {
  awk -v RS=: '
    length($0) && !seen[$0]++ {
      if (out == "") out = $0; else out = out ":" $0
    }
    END { print out }
  '
}

DEFAULT_RENDER_PATH="${HOME_DIR}/.local/bin:${HOME_DIR}/.grok/bin:${HOME_DIR}/bin:${HOME_DIR}/.claude/skills/goal-flight/bin:${SKILL_ROOT}/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
if [ -n "${GOALFLIGHT_DRAIN_PATH:-}" ]; then
  RENDER_PATH="$GOALFLIGHT_DRAIN_PATH"
else
  RENDER_PATH="${DEFAULT_RENDER_PATH}${PATH:+:${PATH}}"
fi
RENDER_PATH="$(printf '%s' "$RENDER_PATH" | dedupe_path)"

render_plist() {
  HOME_VALUE="$HOME_DIR" \
  PYTHON_VALUE="$PYTHON_BIN" \
  SKILL_ROOT_VALUE="$SKILL_ROOT" \
  LOG_VALUE="$LOG_PATH" \
  PATH_VALUE="$RENDER_PATH" \
  "$PYTHON_BIN" - "$TEMPLATE" <<'PY'
import html
import os
import sys
from pathlib import Path

template = Path(sys.argv[1])
text = template.read_text(encoding="utf-8")
for key in ("HOME", "PYTHON", "SKILL_ROOT", "LOG", "PATH"):
    value = html.escape(os.environ[f"{key}_VALUE"], quote=True)
    text = text.replace(f"@{key}@", value)
print(text, end="")
PY
}

require_launchctl() {
  if command -v launchctl >/dev/null 2>&1; then
    return 0
  fi
  echo "ERROR: launchctl not found; launchd drainer install is macOS-only." >&2
  echo "See protocols/drainer.md for the systemd timer equivalent." >&2
  exit 2
}

launchctl_supports_modern() {
  launchctl help 2>&1 | grep -Eq 'bootstrap|bootout'
}

bootout_agent() {
  if launchctl_supports_modern; then
    launchctl bootout "$LAUNCH_DOMAIN" "$PLIST_PATH" >/dev/null 2>&1 || true
  else
    launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
  fi
}

bootstrap_agent() {
  if launchctl_supports_modern; then
    launchctl bootstrap "$LAUNCH_DOMAIN" "$PLIST_PATH"
    launchctl kickstart -k "${LAUNCH_DOMAIN}/${LABEL}"
  else
    launchctl load "$PLIST_PATH"
    launchctl kickstart -k "$LABEL" >/dev/null 2>&1 || true
  fi
}

if [ "$DRY_RUN" -eq 1 ]; then
  render_plist
  exit 0
fi

if [ "$STATUS" -eq 1 ]; then
  require_launchctl
  if launchctl list "$LABEL" >/dev/null 2>&1; then
    echo "${LABEL}: loaded"
    exit 0
  fi
  echo "${LABEL}: not loaded"
  exit 1
fi

if [ "$UNINSTALL" -eq 1 ]; then
  require_launchctl
  bootout_agent
  rm -f "$PLIST_PATH"
  echo "${LABEL}: uninstalled (${PLIST_PATH})"
  exit 0
fi

require_launchctl
[ -f "$TEMPLATE" ] || { echo "ERROR: missing template: $TEMPLATE" >&2; exit 2; }

mkdir -p "$(dirname "$PLIST_PATH")" "$(dirname "$LOG_PATH")"
render_plist > "$PLIST_PATH"

if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$PLIST_PATH" >/dev/null
fi

bootout_agent
bootstrap_agent

echo "${LABEL}: installed"
echo "plist: ${PLIST_PATH}"
echo "skill-root: ${SKILL_ROOT}"
echo "log: ${LOG_PATH}"
