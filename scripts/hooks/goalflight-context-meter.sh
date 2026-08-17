#!/bin/sh

# Claude Code PostToolUse hook. Emits one additionalContext line when the
# session transcript crosses 80/90/95% of the resolved context window so
# the controller can write the RESUME handoff before compaction fires.
#
# Fail silent: a thrown hook must never break the session. Silence is also
# the healthy-state output — nothing prints below 80%, on an unknown
# reading, or when a band has already fired.
#
# The meter resolves the window from the newest assistant model in the
# transcript (fleet map) or GOALFLIGHT_CONTEXT_WINDOW. It is on by
# default. GOALFLIGHT_CONTEXT_METER=0 is the opt-out.
#
# A previous env-window guard skipped the interpreter when the export
# was unset. That made the export mandatory and defeated model-from-
# transcript. The cost it avoided was real (python3 on matcher '.*' =
# ~43ms per tool call), so this file keeps a SHELL-SIDE 1-in-20 call
# throttle instead. Growth-based recheck and band de-dupe stay in
# Python; this script does not parse stdin JSON.

resolve_repo_root() {
  hook_src=${1:-$0}
  hops=0
  while [ -L "$hook_src" ] && [ "$hops" -lt 40 ]; do
    hops=$((hops + 1))
    link_target=$(readlink "$hook_src" 2>/dev/null || true)
    [ -n "$link_target" ] || break
    case "$link_target" in
      /*) hook_src=$link_target ;;
      *) hook_src=$(cd "$(dirname "$hook_src")" 2>/dev/null && pwd)/$link_target ;;
    esac
  done
  cd "$(dirname "$hook_src")/../.." 2>/dev/null && pwd
}

meter_opted_out() {
  case "${GOALFLIGHT_CONTEXT_METER:-}" in
    0|false|FALSE|False|off|OFF|Off) return 0 ;;
  esac
  return 1
}

shell_every() {
  every=${GOALFLIGHT_CONTEXT_METER_EVERY:-20}
  case "$every" in
    ''|*[!0-9]*) every=20 ;;
  esac
  if [ "$every" -lt 1 ]; then
    every=20
  fi
  printf '%s' "$every"
}

counter_file_path() {
  if [ -n "${GOALFLIGHT_CONTEXT_METER_CALLS:-}" ]; then
    printf '%s' "$GOALFLIGHT_CONTEXT_METER_CALLS"
    return
  fi
  state_dir=${GOALFLIGHT_CONTEXT_METER_STATE_DIR:-}
  if [ -z "$state_dir" ]; then
    if [ -n "${GOALFLIGHT_STATE_DIR:-}" ]; then
      state_dir="${GOALFLIGHT_STATE_DIR}/context-meter"
    else
      uid=$(id -u 2>/dev/null || echo 0)
      state_dir="/tmp/goal-flight-${uid}/context-meter"
    fi
  fi
  printf '%s' "${state_dir}/hook-calls"
}

# Return 0 if this call should spawn Python (sampled), 1 to skip.
shell_should_spawn() {
  every=$(shell_every)
  if [ "$every" -le 1 ]; then
    return 0
  fi
  counter_file=$(counter_file_path)
  counter_dir=$(dirname "$counter_file")
  mkdir -p "$counter_dir" 2>/dev/null || true
  calls=0
  if [ -f "$counter_file" ]; then
    calls=$(cat "$counter_file" 2>/dev/null || echo 0)
  fi
  case "$calls" in
    ''|*[!0-9]*) calls=0 ;;
  esac
  calls=$((calls + 1))
  printf '%s\n' "$calls" > "$counter_file" 2>/dev/null || true
  # Call 1, 21, 41, ... so the first tool call can still fire 80%.
  remainder=$((calls % every))
  [ "$remainder" -eq 1 ]
}

main() {
  if meter_opted_out; then
    return 0
  fi
  if ! shell_should_spawn; then
    return 0
  fi

  if [ "${1:-}" = "--dry-run" ]; then
    shift
  fi
  if [ "$#" -gt 0 ]; then
    input_json=$(cat "$1" 2>/dev/null) || input_json=""
  else
    input_json=$(cat 2>/dev/null) || input_json=""
  fi

  plugin_root=$(resolve_repo_root "$0" 2>/dev/null || true)
  [ -n "$plugin_root" ] || return 0
  [ "$plugin_root" != "/" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  meter="$plugin_root/scripts/goalflight_context_meter.py"
  [ -f "$meter" ] || return 0

  printf '%s' "$input_json" | python3 "$meter" --hook 2>/dev/null || true
}

main "$@" 2>/dev/null || true
exit 0
