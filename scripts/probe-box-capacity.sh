#!/usr/bin/env bash
# probe-box-capacity.sh — capture box capacity + ACP-worker availability
#
# Writes a Markdown env-caveats file suitable for the goal-flight dispatch
# wrapper Layer 4 (env caveats). Idempotent — re-running just refreshes the
# data with a new timestamp.
#
# Usage: probe-box-capacity.sh [output_path]
#   default output: ./docs-private/env-caveats.md
#
# Portable across macOS (sysctl) and Linux (/proc/meminfo + nproc).

set -euo pipefail

OUTPUT="${1:-./docs-private/env-caveats.md}"
mkdir -p "$(dirname "$OUTPUT")"

# Detect OS
OS=$(uname -s)
case "$OS" in
  Darwin) ;;
  Linux)  ;;
  *) echo "warning: unsupported OS $OS — capacity probe may be inaccurate" >&2 ;;
esac

# RAM (bytes)
if [ "$OS" = "Darwin" ]; then
  RAM_BYTES=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
  CPU_TOTAL=$(sysctl -n hw.ncpu 2>/dev/null || echo 0)
  CPU_PERF=$(sysctl -n hw.perflevel0.physicalcpu 2>/dev/null || echo 0)
  CPU_EFF=$(sysctl -n hw.perflevel1.physicalcpu 2>/dev/null || echo 0)
  ARCH=$(uname -m)
elif [ "$OS" = "Linux" ]; then
  RAM_KB=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
  RAM_BYTES=$((RAM_KB * 1024))
  CPU_TOTAL=$(nproc 2>/dev/null || echo 0)
  CPU_PERF=0
  CPU_EFF=0
  ARCH=$(uname -m)
else
  RAM_BYTES=0; CPU_TOTAL=0; CPU_PERF=0; CPU_EFF=0
  ARCH=$(uname -m)
fi

RAM_GB=$(awk "BEGIN {printf \"%.1f\", $RAM_BYTES / 1024 / 1024 / 1024}")
RAM_MB=$((RAM_BYTES / 1024 / 1024))

# ACP-worker availability — verify the actual ACP entry point, not just the binary.
# For grok specifically, `grok` on PATH doesn't guarantee `grok agent stdio`
# is a supported subcommand (older versions don't have it). For codex-acp /
# cursor-agent / claude-code-cli-acp the binary name == the ACP entry, so a
# PATH check is sufficient.
probe_worker() {
  local name="$1" cmd="$2" verify_args="${3:-}"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    printf -- "- %-24s missing (\`%s\` not on PATH)\n" "$name" "$cmd"
    return
  fi
  local path
  path=$(command -v "$cmd")
  if [ -n "$verify_args" ]; then
    # Verify the ACP-mode subcommand actually responds. 5s budget is plenty
    # for a --help; longer than that suggests the binary stalls (a real
    # availability problem the controller should know about).
    if timeout 5 "$cmd" $verify_args >/dev/null 2>&1; then
      printf -- "- %-24s **available** — \`%s\` (verified: \`%s %s\`)\n" "$name" "$path" "$cmd" "$verify_args"
    else
      printf -- "- %-24s binary present at \`%s\` but \`%s %s\` failed/timed out — ACP mode not usable\n" "$name" "$path" "$cmd" "$verify_args"
    fi
  else
    printf -- "- %-24s **available** — \`%s\`\n" "$name" "$path"
  fi
}

NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)

cat > "$OUTPUT" <<EOF
# Env caveats — facts the executor cannot discover in <5s via Read/Bash

**Captured**: $NOW (via \`scripts/probe-box-capacity.sh\`)
**Platform**: $OS $ARCH

## Box capacity

- RAM: ${RAM_GB} GB (${RAM_MB} MB total)
- CPU: ${CPU_TOTAL} cores$([ "$CPU_PERF" -gt 0 ] && echo " ($CPU_PERF perf + $CPU_EFF efficiency)" || echo "")

## ACP-worker availability

$(probe_worker "codex-acp" "codex-acp" "")
$(probe_worker "grok agent stdio" "grok" "agent --help")
$(probe_worker "cursor-agent acp" "cursor-agent" "")
$(probe_worker "claude-code-cli-acp" "claude-code-cli-acp" "")

## ACP-worker RSS budget (measured 2026-05-18 on this box class — re-measure if box changed)

| Worker | Procs | Idle RSS | Peak RSS | Sizing class |
|---|---|---|---|---|
| grok | 1 | 95 MB | 111 MB | featherweight (Rust single binary) |
| codex-acp | 4 | 313 MB | 386 MB | mid (front-loaded) |
| claude-code-cli-acp | 2 | 56 MB | 614 MB | lazy-loaded (12× growth on first prompt) |
| cursor-agent | 2 | 558 MB | 1203 MB | heavyweight |

To re-measure on a different box: \`python3 test/probe_worker_memory.py\`

## Pool ceiling guidance

Formula: \`(RAM_MB - controller_reserve_MB) // worst_case_worker_RSS_MB\`,
capped at AcpProcessPool default (20).

- This box: $(awk "BEGIN {r=$RAM_MB - 2048; c=int(r/1200); if (c>20) c=20; if (c<1) c=1; print c}") concurrent workers (worst-case cursor mix)
- 16 GB box: 11 concurrent ((16384-2048)/1200 = 11.95 → 11)
- 8 GB box: 5 concurrent ((8192-2048)/1200 = 5.12 → 5)

Server-side rate limits (Claude session limit, ChatGPT Pro tier limits, etc.)
will usually cap concurrency well below the RAM ceiling on big boxes — the
ceiling is informational/safety, not the operating point.

## Goal-flight integration notes

- Dispatch wrapper Layer 4 should reference this file rather than re-deriving.
- A fresh \`/goal-flight init\` re-runs the capacity probe — re-running mid-run
  is safe; output is overwritten with a new timestamp.
- Workers marked **missing** above can still be installed mid-run; re-run
  the probe afterward.
EOF

echo "wrote $OUTPUT"
