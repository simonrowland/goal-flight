#!/usr/bin/env bash
set -euo pipefail

# Dispatch a Codex ACP worker with Goal Flight defaults and file-backed status.

usage() {
  echo "usage: scripts/goalflight_dispatch.sh <prompt-file> [--slug <slug>] [--worktree]" >&2
  exit 1
}

if [[ $# -lt 1 ]]; then
  usage
fi

prompt_file=$1
shift
slug=
worktree_args=(--worktree off)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --slug)
      [[ $# -ge 2 ]] || usage
      slug=$2
      shift 2
      ;;
    --worktree)
      worktree_args=(--worktree create)
      shift
      ;;
    *)
      usage
      ;;
  esac
done

if [[ ! -f "$prompt_file" ]]; then
  echo "prompt file not found: $prompt_file" >&2
  exit 1
fi

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [[ -z "$slug" ]]; then
  slug=$(basename "$prompt_file")
fi
slug=$(printf "%s" "$slug" | tr "/[:space:]" "--" | tr -cd "A-Za-z0-9._-" | cut -c1-48)
if [[ -z "$slug" ]]; then
  slug=dispatch
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
dispatch_id="$slug-$stamp"
run_dir="$root/docs-private/research/$(date -u +%F)-dispatch-$dispatch_id"
status_path="$run_dir/status.json"
stdout_path="$run_dir/acp.stdout.jsonl"
stderr_path="$run_dir/acp.stderr.log"
mkdir -p "$run_dir"

python3 "$root/scripts/goalflight_acp_run.py" \
  --agent codex-acp \
  --cwd "$PWD" \
  "${worktree_args[@]}" \
  --permission-mode auto \
  --permission-allow-tool-title-pattern '.*' \
  --os-sandbox workspace-write \
  --status-json "$status_path" \
  --max-tool-s 1800 \
  --max-quiet-s 3600 \
  --json \
  --prompt "$prompt_file" \
  > "$stdout_path" \
  2> "$stderr_path" &

echo "dispatch-id: $dispatch_id"
echo "status-path: $status_path"
