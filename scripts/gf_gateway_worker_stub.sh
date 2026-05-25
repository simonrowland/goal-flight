#!/usr/bin/env bash
# Phase 0 gateway worker shim — dry-run / probe friendly; no live gateway calls.
set -euo pipefail

AGENT="${GF_GATEWAY_AGENT:-gateway-worker}"
VERSION="goal-flight-gateway-stub/0.1.0"

usage() {
  cat <<EOF
Usage: gf-${AGENT#gf-} [--version|--help|acp stdio|run ...]

Phase 0 stub for gateway worker dispatch probes and dry-run spawn logging.
EOF
}

emit_status() {
  printf 'STATUS: gateway stub ready (%s)\n' "$AGENT"
}

case "${1:-}" in
  --version)
    echo "$VERSION"
    exit 0
    ;;
  --help|-h)
    usage
    exit 0
    ;;
  acp)
    shift
    if [[ "${1:-}" == "stdio" ]]; then
      emit_status
      exit 0
    fi
    echo "unsupported acp mode: ${*:-}" >&2
    exit 2
    ;;
  run)
    shift
    cwd=""
    prompt_file=""
    status_file=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --cwd) cwd="$2"; shift 2 ;;
        --prompt-file) prompt_file="$2"; shift 2 ;;
        --stdout) shift 2 ;;
        --stderr) shift 2 ;;
        --status-file) status_file="$2"; shift 2 ;;
        *) echo "unknown run arg: $1" >&2; exit 2 ;;
      esac
    done
    emit_status
    if [[ -n "$status_file" ]]; then
      mkdir -p "$(dirname "$status_file")"
      cat >"$status_file" <<JSON
{"schema":"goalflight.acp-run.v1","seq":1,"state":"working","detail":"gateway stub run"}
JSON
    fi
    if [[ -n "$prompt_file" && -f "$prompt_file" ]]; then
      echo "RESULT: stub processed $(wc -l <"$prompt_file" | tr -d ' ') prompt lines"
    else
      echo "RESULT: stub run complete"
    fi
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
