#!/usr/bin/env bash
# Post-compaction resume drill — procedural, always runs (fast subset).

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DRILL="$REPO_ROOT/scripts/hosts/controller/compaction_resume_drill.py"
FIXTURE="$REPO_ROOT/tests/fixtures/compaction_handoff/RESUME-NOTES.md"

if ! command -v python3 >/dev/null 2>&1; then
  echo "SKIP  tests/bash/test-compaction-resume-drill.sh (python3 missing)"
  exit 0
fi

if ! python3 "$REPO_ROOT/tests/python/test_compaction_resume_drill.py" > /tmp/compaction-resume-py-$$.out 2>&1; then
  echo "FAIL  tests/bash/test-compaction-resume-drill.sh (hermetic python)"
  cat /tmp/compaction-resume-py-$$.out | sed 's/^/      /'
  exit 1
fi

RESUME="$FIXTURE"
# Prefer newest local handoff for logging only; drill contract uses the fixture.
if [ -d "$REPO_ROOT/docs-private" ]; then
  found="$(find "$REPO_ROOT/docs-private" -name 'RESUME-NOTES*.md' 2>/dev/null | sort | tail -1 || true)"
  if [ -n "$found" ]; then
    echo "INFO  local resume notes present: $found (drill uses fixture)"
  fi
fi

if ! python3 "$DRILL" --directory "$REPO_ROOT" --resume-notes "$RESUME" --fast-tests --json \
  > /tmp/compaction-resume-drill-$$.json 2>/tmp/compaction-resume-drill-$$.err; then
  echo "FAIL  tests/bash/test-compaction-resume-drill.sh (drill)"
  cat /tmp/compaction-resume-drill-$$.err | sed 's/^/      /'
  cat /tmp/compaction-resume-drill-$$.json | sed 's/^/      /'
  exit 1
fi

python3 - <<'PY' /tmp/compaction-resume-drill-$$.json
import json, sys
payload = json.load(open(sys.argv[1]))
assert payload.get("ok"), payload
for check in payload.get("checks") or []:
    assert check.get("ok"), check
print("PASS  tests/bash/test-compaction-resume-drill.sh")
PY

# Optional maintainer full suite (slow)
if [ -n "${GOALFLIGHT_COMPACTION_DRILL_FULL:-}" ]; then
  if ! python3 "$DRILL" --directory "$REPO_ROOT" --resume-notes "$RESUME" --full-tests --json \
    > /tmp/compaction-resume-full-$$.json; then
    echo "FAIL  tests/bash/test-compaction-resume-drill.sh (full tests)"
    cat /tmp/compaction-resume-full-$$.json | sed 's/^/      /'
    exit 1
  fi
  echo "INFO  full test suite passed (GOALFLIGHT_COMPACTION_DRILL_FULL=1)"
fi
