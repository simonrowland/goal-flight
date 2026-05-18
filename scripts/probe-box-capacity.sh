#!/usr/bin/env bash
# Compatibility wrapper. The procedural source of truth is
# scripts/goalflight_capacity.py.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROFILE_JSON="$(python3 "$SCRIPT_DIR/goalflight_capacity.py" profile --json)"

python3 - "$PROFILE_JSON" <<'PY'
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
