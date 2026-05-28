#!/usr/bin/env bash
set -euo pipefail

# Dispatch a read-only Codex recon job with file-backed prompt and logs.

usage() {
  echo "usage: scripts/goalflight_recon.sh <file-or-glob> [--breadth narrow|medium]" >&2
  exit 1
}

if [[ $# -lt 1 ]]; then
  usage
fi

target=$1
shift
breadth=medium

while [[ $# -gt 0 ]]; do
  case "$1" in
    --breadth)
      [[ $# -ge 2 ]] || usage
      breadth=$2
      shift 2
      ;;
    *)
      usage
      ;;
  esac
done

case "$breadth" in
  narrow|medium) ;;
  *) usage ;;
esac

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
slug=$(printf "%s" "$target" | tr "/[:space:]" "--" | tr -cd "A-Za-z0-9._-" | cut -c1-48)
if [[ -z "$slug" ]]; then
  slug=recon
fi
stamp=$(date -u +%Y%m%dT%H%M%SZ)
run_dir="$root/docs-private/research/$(date -u +%F)-recon-$slug-$stamp"
prompt="$run_dir/prompt.md"
stdout_path="$run_dir/codex-recon.final.md"
stderr_path="$run_dir/codex-recon.stderr.log"
mkdir -p "$run_dir"

cat >"$prompt" <<PROMPT
# Goal Flight recon

Target: \`$target\`
Breadth: \`$breadth\`

Read-only recon. Inspect only what is needed. Summarize findings with exact
file paths and line references when available.

Return contract:

READY: $stdout_path

TL;DR: <=3 lines.
PROMPT

(
  cd "$root"
  codex exec --sandbox read-only --dangerously-bypass-approvals-and-sandbox \
    -c 'model_reasoning_effort="xhigh"' \
    --enable web_search_cached \
    "$prompt" \
    < /dev/null \
    > "$stdout_path" \
    2> "$stderr_path"
) &

echo "READY: $stdout_path"
