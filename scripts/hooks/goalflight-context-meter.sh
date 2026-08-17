#!/bin/sh

# Claude Code PostToolUse hook. Emits one additionalContext line when the
# session transcript crosses 80/90/95% of an *explicit* context window so
# the controller can write the RESUME handoff before compaction fires.
#
# Fail silent: a thrown hook must never break the session. Silence is also
# the healthy-state output — nothing prints below 80%, on an unknown
# reading, or when a band has already fired.

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

main() {
  # Decide the opt-in in SHELL, before paying for a Python interpreter.
  #
  # The meter cannot produce a reading without an explicit window: there is no
  # default and KNOWN_MODEL_WINDOWS is deliberately empty, so an unset
  # GOALFLIGHT_CONTEXT_WINDOW already meant "print nothing". Doing that check
  # here rather than after startup is semantically identical and removes the
  # cost for everyone who has not enabled the feature.
  #
  # Measured on an M-series mac, 20 iterations each: bare `python3 -c pass` =
  # 15.7ms; this hook before the guard = 42.9ms per tool call with the variable
  # UNSET (the same 40.9ms it cost when SET, because the check happened inside
  # Python). hooks.json wires this at matcher '.*', so that was ~43ms on EVERY
  # tool call of EVERY downstream session for a dormant feature. The in-Python
  # 20-call/1MB throttle cannot help: it runs after the interpreter has started.
  [ -n "${GOALFLIGHT_CONTEXT_WINDOW:-}" ] || return 0

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
