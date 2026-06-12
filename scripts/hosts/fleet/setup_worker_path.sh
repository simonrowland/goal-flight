#!/usr/bin/env bash
# macOS fleet worker PATH: symlink agent CLIs into ~/.local/bin and ensure shell PATH.
# Fleet SSH probes bootstrap HOME then prepend ~/.local/bin (goalflight_fleet_ssh.py).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$(uname -s)" != Darwin ]]; then
  echo "skip: setup_worker_path.sh is macOS-only"
  exit 0
fi

LOCAL_BIN="${HOME}/.local/bin"
MARKER="# goal-flight worker PATH"

mkdir -p "${LOCAL_BIN}"

link_bin() {
  local name="$1"
  local src="$2"
  local dest="${LOCAL_BIN}/${name}"
  if [[ -z "${src}" || ! -e "${src}" ]]; then
    return 1
  fi
  if [[ -f "${dest}" && ! -L "${dest}" ]]; then
    echo "skip ${name}: ${dest} is a regular file"
    return 0
  fi
  ln -sf "${src}" "${dest}"
  echo "linked ${name} -> ${src}"
}

find_claude_code() {
  local base="${HOME}/Library/Application Support/Claude/claude-code"
  if [[ ! -d "${base}" ]]; then
    return 1
  fi
  local candidate=""
  while IFS= read -r candidate; do
    [[ -x "${candidate}" ]] || continue
    printf '%s\n' "${candidate}"
    return 0
  done < <(
    find "${base}" -path '*/claude.app/Contents/MacOS/claude' -type f 2>/dev/null
    find "${base}" -maxdepth 2 -name claude -type f 2>/dev/null
  )
  return 1
}

find_cursor_agent() {
  if [[ -L "${LOCAL_BIN}/cursor-agent" || -x "${LOCAL_BIN}/cursor-agent" ]]; then
    readlink "${LOCAL_BIN}/cursor-agent" 2>/dev/null || printf '%s\n' "${LOCAL_BIN}/cursor-agent"
    return 0
  fi
  local candidate=""
  candidate="$(
    find "${HOME}/.local/share/cursor-agent/versions" -name cursor-agent -type f 2>/dev/null | sort | tail -1
  )"
  [[ -n "${candidate}" && -x "${candidate}" ]] || return 1
  printf '%s\n' "${candidate}"
}

if claude_src="$(find_claude_code)"; then
  link_bin claude "${claude_src}"
else
  echo "WARN: Claude Code bundle not found under ~/Library/Application Support/Claude/claude-code" >&2
fi

if cursor_src="$(find_cursor_agent)"; then
  link_bin cursor-agent "${cursor_src}"
else
  echo "WARN: cursor-agent not found under ~/.local/bin or ~/.local/share/cursor-agent" >&2
fi

link_bin grok "${HOME}/.grok/bin/grok" || echo "WARN: grok not installed (~/.grok/bin/grok)" >&2

for brew_name in codex codex-acp opencode claude-code-cli-acp; do
  for prefix in /opt/homebrew/bin /usr/local/bin; do
    if [[ -x "${prefix}/${brew_name}" ]]; then
      link_bin "${brew_name}" "${prefix}/${brew_name}"
      break
    fi
  done
done

profile="${HOME}/.zprofile"
if [[ ! -f "${profile}" ]] || ! grep -Fq "${MARKER}" "${profile}" 2>/dev/null; then
  cat >>"${profile}" <<EOF

${MARKER}
export PATH="\${HOME}/.local/bin:\${HOME}/.grok/bin:/opt/homebrew/bin:/usr/local/bin:\${PATH}"
EOF
  echo "appended PATH block to ${profile}"
else
  echo "PATH block already present in ${profile}"
fi

export PATH="${LOCAL_BIN}:${HOME}/.grok/bin:/opt/homebrew/bin:/usr/local/bin:${PATH:-}"

if [[ "${GOALFLIGHT_SKIP_ACP_VENV_SETUP:-0}" == "1" ]]; then
  echo "skip: ACP venv setup disabled by GOALFLIGHT_SKIP_ACP_VENV_SETUP"
elif [[ -x "${SCRIPT_DIR}/ensure_acp_venv.sh" ]]; then
  bash "${SCRIPT_DIR}/ensure_acp_venv.sh"
else
  echo "WARN: ensure_acp_venv.sh not found beside setup_worker_path.sh" >&2
fi

echo "PATH check (~/.local/bin probes):"
for bin in codex codex-acp claude claude-code-cli-acp cursor-agent grok opencode; do
  if [[ -x "${LOCAL_BIN}/${bin}" ]]; then
    echo "  ok ${bin}=${LOCAL_BIN}/${bin}"
  elif command -v "${bin}" >/dev/null 2>&1; then
    echo "  ok ${bin}=$(command -v "${bin}") (not under ~/.local/bin)"
  else
    echo "  missing ${bin}"
  fi
done
