#!/usr/bin/env bash
# Install the out-of-session launchd producers for fleet-console data planes.

set -euo pipefail

LABEL_PREFIX="com.goalflight.fleet-console"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEMPLATE="${REPO_ROOT}/scripts/templates/${LABEL_PREFIX}.plist.tmpl"

DRY_RUN=0
UNINSTALL=0
STATUS=0
SELECTED_PLANE=""
SKILL_ROOT="${SKILL_ROOT:-${GOALFLIGHT_SKILL_ROOT:-}}"

usage() {
  cat <<'EOF'
Usage:
  scripts/install-fleet-console.sh [--skill-root <path>] [--plane attention|fleet] [--dry-run]
  scripts/install-fleet-console.sh [--plane attention|fleet] --status
  scripts/install-fleet-console.sh [--plane attention|fleet] --uninstall

Installs two per-user launchd agents by default:
  attention  every 5s, with a 3s wall-clock budget
  fleet      every 30s, with a 4s wall-clock budget

Environment:
  SKILL_ROOT or GOALFLIGHT_SKILL_ROOT       override ~/.goal-flight/skill
  GOALFLIGHT_FLEET_CONSOLE_OUTPUT_DIR       override <skill-root>/templates/fleet-console
  GOALFLIGHT_FLEET_CONSOLE_CONFIG           override ~/.goal-flight/fleet-console-output-dir
  GOALFLIGHT_FLEET_CONSOLE_LOCK_DIR         override ~/.goal-flight/locks
  GOALFLIGHT_FLEET_CONSOLE_LOG_DIR          override ~/.goal-flight
  GOALFLIGHT_FLEET_CONSOLE_PATH             override rendered launchd PATH
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --status) STATUS=1 ;;
    --plane)
      shift
      [ "$#" -gt 0 ] || { echo "ERROR: --plane needs attention or fleet" >&2; exit 2; }
      SELECTED_PLANE="$1"
      ;;
    --plane=*) SELECTED_PLANE="${1#--plane=}" ;;
    --skill-root)
      shift
      [ "$#" -gt 0 ] || { echo "ERROR: --skill-root needs a path" >&2; exit 2; }
      SKILL_ROOT="$1"
      ;;
    --skill-root=*) SKILL_ROOT="${1#--skill-root=}" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

mode_count=$((DRY_RUN + UNINSTALL + STATUS))
if [ "$mode_count" -gt 1 ]; then
  echo "ERROR: choose only one of --dry-run, --uninstall, or --status" >&2
  exit 2
fi
if [ -n "$SELECTED_PLANE" ] && [ "$SELECTED_PLANE" != "attention" ] && [ "$SELECTED_PLANE" != "fleet" ]; then
  echo "ERROR: --plane needs attention or fleet" >&2
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
OUTPUT_DIR="$(expand_home "${GOALFLIGHT_FLEET_CONSOLE_OUTPUT_DIR:-${SKILL_ROOT}/templates/fleet-console}")"
CONFIG_PATH="$(expand_home "${GOALFLIGHT_FLEET_CONSOLE_CONFIG:-${HOME_DIR}/.goal-flight/fleet-console-output-dir}")"
LOCK_DIR="$(expand_home "${GOALFLIGHT_FLEET_CONSOLE_LOCK_DIR:-${HOME_DIR}/.goal-flight/locks}")"
LOG_DIR="$(expand_home "${GOALFLIGHT_FLEET_CONSOLE_LOG_DIR:-${HOME_DIR}/.goal-flight}")"
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

DEFAULT_RENDER_PATH="${HOME_DIR}/.local/bin:${HOME_DIR}/.grok/bin:${HOME_DIR}/bin:${SKILL_ROOT}/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
if [ -n "${GOALFLIGHT_FLEET_CONSOLE_PATH:-}" ]; then
  RENDER_PATH="$GOALFLIGHT_FLEET_CONSOLE_PATH"
else
  RENDER_PATH="${DEFAULT_RENDER_PATH}${PATH:+:${PATH}}"
fi
RENDER_PATH="$(printf '%s' "$RENDER_PATH" | dedupe_path)"

planes() {
  if [ -n "$SELECTED_PLANE" ]; then
    printf '%s\n' "$SELECTED_PLANE"
  else
    printf '%s\n' attention fleet
  fi
}

plane_values() {
  PLANE_VALUE="$1"
  LABEL_VALUE="${LABEL_PREFIX}.${PLANE_VALUE}"
  OUTPUT_VALUE="${OUTPUT_DIR}/${PLANE_VALUE}-data.js"
  LOG_VALUE="${LOG_DIR}/fleet-console-${PLANE_VALUE}-launchd.log"
  case "$PLANE_VALUE" in
    attention)
      # Live read-only sample on 1,404 local rows / 1,954 registered projects
      # measured 1.12s through the deployed wrapper (2026-08-16).
      # Budget = ceil(2 × 1.12s) = 3s.
      # Reserve = 1s for termination + atomic DEGRADED publication; cadence
      # sanity: 5s > 3s budget + 1s reserve.
      INTERVAL_VALUE=5
      BUDGET_VALUE=3
      ;;
    fleet)
      # Budget from the PRODUCTION quantity under launchd, which runs slower
      # than an interactive shell: the same tick measured 7.0s by hand and
      # >14s under the agent on 2026-08-16 (cold caches, contended CPU).
      # Interval 60s, budget 30s = ~2x the worst observed tick with 30s of
      # headroom for termination and atomic publication. Sanity: 14s < 30s <
      # 60s. Profiling the remaining scan cost is tracked in the store.
      INTERVAL_VALUE=60
      BUDGET_VALUE=30
      ;;
  esac
  PLIST_PATH_VALUE="${HOME_DIR}/Library/LaunchAgents/${LABEL_VALUE}.plist"
}

render_plist() {
  plane_values "$1"
  HOME_VALUE="$HOME_DIR" \
  PYTHON_VALUE="$PYTHON_BIN" \
  SKILL_ROOT_VALUE="$SKILL_ROOT" \
  LOCK_DIR_VALUE="$LOCK_DIR" \
  PATH_VALUE="$RENDER_PATH" \
  PLANE_VALUE="$PLANE_VALUE" \
  LABEL_VALUE="$LABEL_VALUE" \
  OUTPUT_VALUE="$OUTPUT_VALUE" \
  LOG_VALUE="$LOG_VALUE" \
  INTERVAL_VALUE="$INTERVAL_VALUE" \
  BUDGET_VALUE="$BUDGET_VALUE" \
  "$PYTHON_BIN" - "$TEMPLATE" <<'PY'
import html
import os
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
for key in (
    "HOME", "PYTHON", "SKILL_ROOT", "LOCK_DIR", "PATH", "PLANE",
    "LABEL", "OUTPUT", "LOG", "INTERVAL", "BUDGET",
):
    value = html.escape(os.environ[f"{key}_VALUE"], quote=True)
    text = text.replace(f"@{key}@", value)
print(text, end="")
PY
}

require_launchctl() {
  if command -v launchctl >/dev/null 2>&1; then
    return 0
  fi
  echo "ERROR: launchctl not found; fleet-console producer install is macOS-only." >&2
  exit 2
}

launchctl_supports_modern() {
  launchctl help 2>&1 | grep -Eq 'bootstrap|bootout'
}

bootout_agent() {
  if launchctl_supports_modern; then
    launchctl bootout "$LAUNCH_DOMAIN" "$PLIST_PATH_VALUE" >/dev/null 2>&1 || true
  else
    launchctl unload "$PLIST_PATH_VALUE" >/dev/null 2>&1 || true
  fi
}

bootstrap_agent() {
  if launchctl_supports_modern; then
    launchctl bootstrap "$LAUNCH_DOMAIN" "$PLIST_PATH_VALUE"
    launchctl kickstart -k "${LAUNCH_DOMAIN}/${LABEL_VALUE}"
  else
    launchctl load "$PLIST_PATH_VALUE"
    launchctl kickstart -k "$LABEL_VALUE" >/dev/null 2>&1 || true
  fi
}

if [ "$DRY_RUN" -eq 1 ]; then
  [ -f "$TEMPLATE" ] || { echo "ERROR: missing template: $TEMPLATE" >&2; exit 2; }
  first=1
  while IFS= read -r plane; do
    if [ "$first" -eq 0 ]; then printf '\n'; fi
    if [ -z "$SELECTED_PLANE" ]; then printf '<!-- %s -->\n' "${LABEL_PREFIX}.${plane}"; fi
    render_plist "$plane"
    first=0
  done <<EOF
$(planes)
EOF
  exit 0
fi

if [ "$STATUS" -eq 1 ]; then
  require_launchctl
  result=0
  while IFS= read -r plane; do
    plane_values "$plane"
    if launchctl list "$LABEL_VALUE" >/dev/null 2>&1; then
      echo "${LABEL_VALUE}: loaded"
    else
      echo "${LABEL_VALUE}: not loaded"
      result=1
    fi
  done <<EOF
$(planes)
EOF
  exit "$result"
fi

if [ "$UNINSTALL" -eq 1 ]; then
  require_launchctl
  while IFS= read -r plane; do
    plane_values "$plane"
    bootout_agent
    rm -f "$PLIST_PATH_VALUE"
    echo "${LABEL_VALUE}: uninstalled (${PLIST_PATH_VALUE})"
  done <<EOF
$(planes)
EOF
  if [ -z "$SELECTED_PLANE" ]; then
    rm -f "$CONFIG_PATH"
    echo "fleet-console output config removed (${CONFIG_PATH})"
  fi
  exit 0
fi

require_launchctl
[ -f "$TEMPLATE" ] || { echo "ERROR: missing template: $TEMPLATE" >&2; exit 2; }
mkdir -p "${HOME_DIR}/Library/LaunchAgents" "$LOG_DIR" "$LOCK_DIR" "$OUTPUT_DIR" "$(dirname "$CONFIG_PATH")"
while IFS= read -r plane; do
  plane_values "$plane"
  render_plist "$plane" > "$PLIST_PATH_VALUE"
  if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$PLIST_PATH_VALUE" >/dev/null
  fi
  bootout_agent
  bootstrap_agent
  echo "${LABEL_VALUE}: installed"
  echo "plist: ${PLIST_PATH_VALUE}"
  echo "output: ${OUTPUT_VALUE}"
  echo "log: ${LOG_VALUE}"
done <<EOF
$(planes)
EOF
CONFIG_TMP="$(mktemp "${CONFIG_PATH}.tmp.XXXXXX")"
trap 'if [ -n "${CONFIG_TMP:-}" ]; then rm -f -- "$CONFIG_TMP"; fi' EXIT
printf '%s\n' "$OUTPUT_DIR" > "$CONFIG_TMP"
chmod 600 "$CONFIG_TMP"
mv "$CONFIG_TMP" "$CONFIG_PATH"
CONFIG_TMP=""
trap - EXIT
echo "output config: ${CONFIG_PATH}"
