#!/usr/bin/env bash
# install-grok-trust.sh — register a project as trusted for one grok home.
#
# Grok refuses to operate in a directory it has not been told to trust, and it
# does so by exiting almost immediately with nothing on stdout. Through the
# dispatcher that presents as a worker which "launches but dies in seconds" with
# an empty tail and no surfaced error, which is indistinguishable at a glance
# from a broken login. It is not a login problem: a freshly created account home
# simply starts with an EMPTY trust list.
#
# That matters most for per-account seats. The host ~/.grok accumulates trust
# decisions interactively over time, so it quietly works; a new seat under
# ~/.goal-flight/accounts/<seat>/grok trusts nothing at all, and every repo has
# to be registered again for that seat.
#
# This is the grok counterpart of install-codex-overrides.sh and copies its path
# guards. Trust is a boundary: this script will not register root, $HOME, or a
# single-segment system path.
#
# Usage:
#   install-grok-trust.sh [--home <grok-home>] [--seat <label>] [--project <path>] [--check]
#
#   --home <path>     the HOME whose .grok/ is edited (default: $HOME)
#   --seat <label>    shorthand for --home ~/.goal-flight/accounts/<label>/grok
#   --project <path>  project to trust (default: git toplevel, else pwd)
#   --check           report whether it is already trusted; change nothing
#
# Exit: 0 registered or already trusted; 1 --check found it untrusted; 2 refused.

set -euo pipefail

GROK_HOME_DIR="$HOME"
PROJECT_ROOT=""
CHECK_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --home) GROK_HOME_DIR="$2"; shift 2 ;;
    --seat) GROK_HOME_DIR="$HOME/.goal-flight/accounts/$2/grok"; shift 2 ;;
    --project) PROJECT_ROOT="$2"; shift 2 ;;
    --check) CHECK_ONLY=1; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$PROJECT_ROOT" ]; then
  PROJECT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
fi

# Guards, idempotence, and the file format live in grok_seats.py so the
# dispatcher (which registers trust automatically before a grok launch) and this
# command cannot drift apart. This is a thin front end, not a second copy.
HERE="$(cd "$(dirname "$0")" && pwd)"
exec python3 - "$HERE" "$GROK_HOME_DIR" "$PROJECT_ROOT" "$CHECK_ONLY" <<'PYEOF'
import sys
from pathlib import Path

here, home_dir, project_root, check_only = sys.argv[1:5]
sys.path.insert(0, here)
import grok_seats

try:
    resolved = grok_seats._trust_guard(Path(project_root))
except grok_seats.TrustRefused as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    raise SystemExit(2)

if not resolved.is_dir():
    print(f"ERROR: project path does not exist: {resolved}", file=sys.stderr)
    raise SystemExit(2)

trust_file = Path(home_dir).expanduser() / ".grok" / "trusted_folders.toml"
already = grok_seats.is_project_trusted(Path(home_dir).expanduser(), resolved)

if check_only == "1":
    print(("CHECK: already trusted: " if already else "CHECK: NOT trusted: ")
          + f"{resolved}  ({trust_file})")
    raise SystemExit(0 if already else 1)

if already:
    print(f"already trusted: {resolved}  ({trust_file})")
    raise SystemExit(0)

if not trust_file.parent.is_dir():
    print(f"ERROR: {trust_file.parent} does not exist.", file=sys.stderr)
    print(f"  Log in first:  HOME={home_dir} grok", file=sys.stderr)
    raise SystemExit(2)

grok_seats.ensure_project_trusted(Path(home_dir).expanduser(), resolved)
print(f"trusted: {resolved}  ({trust_file})")
PYEOF
