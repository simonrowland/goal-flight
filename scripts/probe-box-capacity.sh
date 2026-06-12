#!/usr/bin/env bash
# Compatibility wrapper. The procedural source of truth is
# scripts/goalflight_capacity.py.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# GOALFLIGHT_PYTHON is accepted-watch per the SC-13 sweep: interpreter selector only.
if [[ -n "${GOALFLIGHT_PYTHON:-}" ]]; then
  PY="$GOALFLIGHT_PYTHON"
else
  PY3_CANDIDATE="python${GOALFLIGHT_PYTHON_MAJOR:-3}"
  if command -v "$PY3_CANDIDATE" >/dev/null 2>&1; then
    PY="$PY3_CANDIDATE"
  elif command -v python >/dev/null 2>&1; then
    PY="python"
  else
    echo "probe-box-capacity.sh: Python 3 not found; set GOALFLIGHT_PYTHON" >&2
    exit 127
  fi
fi
PROFILE_JSON="$("$PY" "$SCRIPT_DIR/goalflight_capacity.py" profile --json)"

"$PY" - "$PROFILE_JSON" <<'PY'
import json
import sys

p = json.loads(sys.argv[1])
print("# Goal-flight box capacity")
print()
print(f"- Machine: {p['machine_id']}")
print(f"- RAM: {p['ram_mb'] / 1024:.1f} GB ({p['ram_mb']} MB total)")
print(f"- CPU: {p['cpu_count']}")
print(f"- Raw RAM ceiling: {p['raw_ram_ceiling']} workers")
print(f"- Operating cap: {p['operating_cap']} workers")
print()
print("## Worker availability")
for name, available in sorted(p["tools"].items()):
    print(f"- {name}: {'present' if available else 'missing'}")
print()
print("## Worker RSS budget")
for name, rss in sorted(p["agent_rss_mb"].items()):
    print(f"- {name}: {rss} MB")
print()
print("## Integration notes")
print("- Raw RAM ceiling is a safety bound, not the desired concurrency.")
print("- Multiple sessions coordinate through goalflight_capacity.py leases.")
print("- Re-run this wrapper only for human-readable env caveats.")
PY
