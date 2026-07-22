#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
SETUP="$REPO_ROOT/setup.sh"
WORKER_PATH="${REPO_ROOT}/scripts/hosts/fleet/setup_worker_path.sh"
TRAITS_SCRIPT="$REPO_ROOT/scripts/goalflight_agent_traits.py"

WITH_AGENT_TRAITS=0
DETECTED_HOST=""

for arg in "$@"; do
  if [[ "$arg" == --with-agent-traits ]]; then
    WITH_AGENT_TRAITS=1
  fi
done

ensure_mac_worker_bins() {
  if [[ "$(uname -s)" == Darwin && -x "${WORKER_PATH}" ]]; then
    bash "${WORKER_PATH}"
  fi
}

report_gstack_browser_readiness() {
  # Cover Claude-host and canonical ~/.gstack installs (ADAPTER-4).
  local browser=""
  local candidate
  if [[ -n "${GSTACK_BROWSE_BIN:-}" && -x "${GSTACK_BROWSE_BIN}" ]]; then
    browser="${GSTACK_BROWSE_BIN}"
  else
    for candidate in \
      "${HOME:-}/.claude/skills/gstack/browse/dist/browse" \
      "${HOME:-}/.gstack/repos/gstack/browse/dist/browse"
    do
      if [[ -x "$candidate" ]]; then
        browser="$candidate"
        break
      fi
    done
  fi
  if [[ -n "$browser" ]]; then
    printf 'NOTE gstack-browser: present (%s)\n' "$browser"
  else
    printf '%s\n' \
      'NOTE gstack-browser: absent; optional web-QA addon. Build with: (cd ~/.claude/skills/gstack/browse && bun install && bun run build) or (cd ~/.gstack/repos/gstack/browse && bun install && bun run build)'
  fi
}

detect_host_from_args() {
  local sub="${1:-}"
  case "$sub" in
    codex)
      DETECTED_HOST=codex
      ;;
    claude-acp)
      DETECTED_HOST=claude
      ;;
    *)
      local arg prev=""
      for arg in "$@"; do
        case "$arg" in
          --codex-install)
            DETECTED_HOST=codex
            return 0
            ;;
          --agent=codex)
            DETECTED_HOST=codex
            return 0
            ;;
          --agent=claude)
            DETECTED_HOST=claude
            return 0
            ;;
        esac
        if [[ "$prev" == --agent && "$arg" == codex ]]; then
          DETECTED_HOST=codex
          return 0
        fi
        if [[ "$prev" == --agent && "$arg" == claude ]]; then
          DETECTED_HOST=claude
          return 0
        fi
        prev="$arg"
      done
      ;;
  esac
}

maybe_offer_agent_traits() {
  local host="${DETECTED_HOST:-}"
  if [[ -z "$host" ]]; then
    if [[ "$WITH_AGENT_TRAITS" -eq 1 ]]; then
      host=claude
    else
      return 0
    fi
  fi

  local target=""
  target="$(python3 -c "import sys; sys.path.insert(0, '${REPO_ROOT}/scripts'); import goalflight_agent_traits as t; p=t.default_target('${host}'); print(p or '')")"
  if [[ -z "$target" ]]; then
    return 0
  fi

  local do_install=0
  if [[ "$WITH_AGENT_TRAITS" -eq 1 ]]; then
    do_install=1
  elif [[ -t 0 ]]; then
    local titles
    titles="$(python3 -c "import sys; sys.path.insert(0, '${REPO_ROOT}/scripts'); import goalflight_agent_traits as t; print('\\n'.join('  - ' + s for s in t.section_titles()))")"
    printf '%s\n' \
      "Optional general agent-behavior traits (goal-flight):" \
      "$titles" \
      "" \
      "Add these general agent-behavior traits to ${target}? They load even when the skill is unloaded; a backup is written and you can remove the marked block anytime. [y/N]"
    local reply=""
    read -r reply || reply=""
    case "$reply" in
      [yY]|[yY][eE][sS]) do_install=1 ;;
    esac
  fi

  if [[ "$do_install" -eq 0 ]]; then
    return 0
  fi

  set +e
  local out rc
  out="$(python3 "$TRAITS_SCRIPT" --install --host "$host" --json 2>&1)"
  rc=$?
  set -e
  if [[ "$rc" -ne 0 ]]; then
    printf 'AGENT_TRAITS skipped (install helper failed): %s\n' "$out" >&2
    return 0
  fi
  local action backup
  action="$(printf '%s' "$out" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('action','?'))")"
  backup="$(printf '%s' "$out" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('backup') or '')")"
  printf 'AGENT_TRAITS action=%s target=%s' "$action" "$target"
  if [[ -n "$backup" ]]; then
    printf ' backup=%s' "$backup"
  fi
  printf '\n'
}

run_setup_and_traits() {
  local -a setup_args=()
  local arg
  for arg in "$@"; do
    case "$arg" in
      --with-agent-traits) ;;
      *) setup_args+=("$arg") ;;
    esac
  done
  "$SETUP" "${setup_args[@]}"
  local rc=$?
  maybe_offer_agent_traits || true
  report_gstack_browser_readiness || true
  return "$rc"
}

if [[ $# -ge 1 ]]; then
  detect_host_from_args "$1" "$@"
  case "$1" in
    cursor)
      shift
      if [[ $# -ge 1 && "$1" != -* ]]; then
        project=$1
        shift
      else
        project="."
      fi
      run_setup_and_traits --cursor-install "$project" "$@"
      ensure_mac_worker_bins
      exit $?
      ;;
    opencode)
      shift
      if [[ $# -ge 1 && "$1" != -* ]]; then
        project=$1
        shift
      else
        project="."
      fi
      run_setup_and_traits --opencode-install "$project" "$@"
      ensure_mac_worker_bins
      exit $?
      ;;
    codex)
      shift
      run_setup_and_traits --codex-install "$@"
      ensure_mac_worker_bins
      exit $?
      ;;
    grok)
      shift
      bash "${REPO_ROOT}/scripts/hosts/fleet/install_grok.sh" "$@"
      exit $?
      ;;
    claude-acp)
      shift
      bash "${REPO_ROOT}/scripts/hosts/fleet/install_claude_acp.sh" "$@"
      rc=$?
      maybe_offer_agent_traits || true
      report_gstack_browser_readiness || true
      exit "$rc"
      ;;
    worker-path)
      shift
      ensure_mac_worker_bins
      exit $?
      ;;
  esac
fi

detect_host_from_args "" "$@"
run_setup_and_traits "$@"
