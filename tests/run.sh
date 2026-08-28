#!/usr/bin/env bash
# Discover and run test suites:
#   - tests/bash/test-*.sh — bash tests (installers, codex overrides, fork-detect)
#   - tests/python/**/test_*.py — isolated Python modules routed by pytest
#   - tests/js/test_*.js — Node-only hermetic browserless checks
# One pass/fail per bash/JS file plus one pass/fail for the isolated Python suite.
# Exit code = number of failed gate entries.
#
# Skips tests/python/dispatch_acp_chunk.py (live e2e against real codex-acp, non-hermetic).

set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Isolate every machine-global writable default. A test that omits an explicit
# messages_dir/journal_dir/fleet_dir/pidfile_dir must never migrate or append to
# the operator's live ~/.goal-flight, ~/.local/state/goal-flight, or
# /tmp/goal-flight-acp-pids.d merely because the production helper supplies
# defaults. Journal isolation is its own knob (GOALFLIGHT_JOURNAL_DIR);
# TASK_STORE_DIR only isolates journals incidentally and a test that pops the
# store override would otherwise write the live XDG journals index. Pidfile
# isolation is GOAL_FLIGHT_PIDFILE_DIR (production name) with the
# GOALFLIGHT_PIDFILE_DIR alias kept in lockstep.
_GF_TEST_ENV_BASE="$(mktemp -d "${TMPDIR:-/tmp}/gf-test-env-XXXXXX")"
trap 'rm -rf "$_GF_TEST_ENV_BASE" 2>/dev/null || true' EXIT
_GF_TASK_STORE_BASE="${GOALFLIGHT_TASK_STORE_DIR:-$_GF_TEST_ENV_BASE/task-store}"
_GF_MESSAGES_BASE="${GOALFLIGHT_MESSAGES_DIR:-$_GF_TEST_ENV_BASE/messages}"
_GF_JOURNAL_BASE="${GOALFLIGHT_JOURNAL_DIR:-$_GF_TEST_ENV_BASE/journal}"
_GF_PIDFILE_BASE="${GOAL_FLIGHT_PIDFILE_DIR:-${GOALFLIGHT_PIDFILE_DIR:-$_GF_TEST_ENV_BASE/pids}}"
# Do not pass the operator's XDG_STATE_HOME through: that is the journals-index
# fallback when JOURNAL_DIR / TASK_STORE_DIR are unset. Preserve it only so the
# live-index snapshot still watches the operator's directory, not the isolate.
_GF_OPERATOR_XDG_STATE_HOME="${XDG_STATE_HOME-}"
_GF_XDG_BASE="$_GF_TEST_ENV_BASE/xdg"

pass=0
fail=0
skip=0
failed_tests=()
skill_structure_collected=0
list_only=0
if [ "${1:-}" = "--list" ]; then
  list_only=1
fi

run_isolated_test_env() {
  # GOALFLIGHT_CAPACITY_CONF -> /dev/null forces the committed baseline caps:
  # /dev/null reads empty, the loader falls back, so a machine with a live
  # per-operator capacity.local.json can't skew suite assertions (same reason
  # the suite isolates GOALFLIGHT_STATE_DIR). An explicit outer value passes
  # through for tests that deliberately exercise a real conf.
  env -u GOALFLIGHT_STEER_FILE -u GOALFLIGHT_ALLOW_EXTERNAL_STEER_FILE \
    -u GOALFLIGHT_ISOLATED_TEST_FILE \
    GOALFLIGHT_CAPACITY_CONF="${GOALFLIGHT_CAPACITY_CONF:-/dev/null}" \
    GOALFLIGHT_MESSAGES_DIR="$_GF_MESSAGES_BASE" \
    GOALFLIGHT_JOURNAL_DIR="$_GF_JOURNAL_BASE" \
    GOALFLIGHT_TASK_STORE_DIR="${GOALFLIGHT_TASK_STORE_DIR:-$_GF_TASK_STORE_BASE}" \
    GOAL_FLIGHT_PIDFILE_DIR="$_GF_PIDFILE_BASE" \
    GOALFLIGHT_PIDFILE_DIR="$_GF_PIDFILE_BASE" \
    XDG_STATE_HOME="$_GF_XDG_BASE" "$@"
}

# Bash tests (tests/bash/test-*.sh)
cd "$SCRIPT_DIR/bash"
for test in test-*.sh; do
  [ -f "$test" ] || continue
  if [ "$list_only" -eq 1 ]; then
    echo "tests/bash/$test"
    continue
  fi
  # Live external-agent probes are slow and environment-flaky; they can wedge the
  # whole suite on a stalled child or an LLM that exits "success" without doing
  # the asked work. Skip EXECUTION by default; opt in explicitly. Listing above
  # is intentionally unaffected so --list collection stays stable regardless of
  # GOALFLIGHT_LIVE_OPENCODE / GOALFLIGHT_LIVE_GROK.
  case "$test" in
    test-opencode-*.sh)
      if [ "${GOALFLIGHT_LIVE_OPENCODE:-0}" != "1" ]; then
        echo "SKIP  tests/bash/$test (live opencode ACP probe; set GOALFLIGHT_LIVE_OPENCODE=1 to run)"
        skip=$((skip + 1))
        continue
      fi
      ;;
    test-grok-*.sh)
      if [ "${GOALFLIGHT_LIVE_GROK:-0}" != "1" ]; then
        echo "SKIP  tests/bash/$test (live grok probe; set GOALFLIGHT_LIVE_GROK=1 to run)"
        skip=$((skip + 1))
        continue
      fi
      ;;
  esac
  if run_isolated_test_env bash "$test" > /tmp/goal-flight-test-$$.out 2>&1; then
    echo "PASS  tests/bash/$test"
    pass=$((pass + 1))
  else
    echo "FAIL  tests/bash/$test"
    cat /tmp/goal-flight-test-$$.out | sed 's/^/      /'
    fail=$((fail + 1))
    failed_tests+=("tests/bash/$test")
  fi
  rm -f /tmp/goal-flight-test-$$.out
done

# Python tests. The directory-level pytest driver routes every module in a fresh
# process: guarded case_* modules run as scripts, native pytest modules run under
# pytest, and nested modules such as tests/python/ext/ stay visible. Directly
# executing every file is not equivalent: files without a main guard exit 0
# without running their test_* functions.
if command -v python3 >/dev/null 2>&1 && [ -d "$REPO_ROOT/tests/python" ]; then
  cd "$REPO_ROOT"
  if [ "$list_only" -eq 1 ]; then
    find tests/python -type f -name 'test_*.py' -print | LC_ALL=C sort
  else
    # Measure collection through the same pytest/conftest contract that owns
    # execution. Name the state precisely: collection is observed here; the
    # driver and its regression test separately guarantee execution.
    if run_isolated_test_env python3 -m pytest tests/python --collect-only -q \
        > /tmp/goal-flight-collect-$$.out 2>&1; then
      skill_structure_collected="$(
        grep -Ec '::test_isolated_test_module\[test_skill_structure\.py\]$' \
          /tmp/goal-flight-collect-$$.out || true
      )"
    fi
    rm -f /tmp/goal-flight-collect-$$.out

    # Snapshot live XDG project-<10-hex> journal slugs so a suite leak cannot
    # hide behind incidental TASK_STORE_DIR isolation of the test's own slug.
    _GF_LIVE_SLUG_SNAP="$(mktemp "${TMPDIR:-/tmp}/gf-live-journal-slugs-XXXXXX")"
    env -u GOALFLIGHT_JOURNAL_DIR -u GOALFLIGHT_TASK_STORE_DIR -u XDG_STATE_HOME \
      python3 - "$_GF_LIVE_SLUG_SNAP" "$_GF_OPERATOR_XDG_STATE_HOME" <<'PY'
import os, re, sys
from pathlib import Path
op = sys.argv[2].strip()
base = Path(op).expanduser() if op else Path.home() / ".local" / "state"
index = base / "goal-flight" / "journals"
pat = re.compile(r"^project-[0-9a-f]{10}$")
names = []
if index.is_dir():
    for child in index.iterdir():
        try:
            if child.is_dir() and pat.match(child.name):
                names.append(child.name)
        except OSError:
            continue
Path(sys.argv[1]).write_text("\n".join(sorted(names)), encoding="utf-8")
PY
    if run_isolated_test_env python3 -m pytest tests/python -q > /tmp/goal-flight-test-$$.out 2>&1; then
      grep '^FLAKE  ' /tmp/goal-flight-test-$$.out | sed 's/^/      /' || true
      echo "PASS  tests/python (isolated pytest directory suite)"
      sed -n '$p' /tmp/goal-flight-test-$$.out | sed 's/^/      /'
      pass=$((pass + 1))
    else
      echo "FAIL  tests/python (isolated pytest directory suite)"
      cat /tmp/goal-flight-test-$$.out | sed 's/^/      /'
      fail=$((fail + 1))
      failed_tests+=("tests/python")
    fi
    rm -f /tmp/goal-flight-test-$$.out
    if ! env -u GOALFLIGHT_JOURNAL_DIR -u GOALFLIGHT_TASK_STORE_DIR -u XDG_STATE_HOME \
      python3 - "$_GF_LIVE_SLUG_SNAP" "$_GF_OPERATOR_XDG_STATE_HOME" <<'PY'
import os, re, sys
from pathlib import Path
before = {
    line.strip()
    for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
    if line.strip()
}
op = sys.argv[2].strip()
base = Path(op).expanduser() if op else Path.home() / ".local" / "state"
index = base / "goal-flight" / "journals"
pat = re.compile(r"^project-[0-9a-f]{10}$")
after = set()
if index.is_dir():
    for child in index.iterdir():
        try:
            if child.is_dir() and pat.match(child.name):
                after.add(child.name)
        except OSError:
            continue
leaked = sorted(after - before)
if leaked:
    print("live journals index gained project-<10-hex> children:", ", ".join(leaked))
    raise SystemExit(1)
PY
    then
      :
    else
      echo "FAIL  live journals index gained project-<10-hex> children during python suite"
      fail=$((fail + 1))
      failed_tests+=("live-journal-isolation")
    fi
    rm -f "$_GF_LIVE_SLUG_SNAP"
  fi
fi

# JS tests (tests/js/test_*.js; skipped when node is unavailable)
if [ -d "$REPO_ROOT/tests/js" ]; then
  cd "$REPO_ROOT"
  for test in tests/js/test_*.js; do
    [ -f "$test" ] || continue
    if [ "$list_only" -eq 1 ]; then
      echo "$test"
      continue
    fi
    if ! command -v node >/dev/null 2>&1; then
      echo "SKIP  $test (node not found on PATH)"
      skip=$((skip + 1))
      continue
    fi
    if run_isolated_test_env node "$test" > /tmp/goal-flight-test-$$.out 2>&1; then
      echo "PASS  $test"
      pass=$((pass + 1))
    else
      echo "FAIL  $test"
      cat /tmp/goal-flight-test-$$.out | sed 's/^/      /'
      fail=$((fail + 1))
      failed_tests+=("$test")
    fi
    rm -f /tmp/goal-flight-test-$$.out
  done
fi

if [ "$list_only" -ne 1 ] && [ "$skill_structure_collected" -ne 1 ]; then
  echo "FAIL  tests/python/test_skill_structure.py"
  echo "      required Golden Master guard was not collected by the pytest driver"
  fail=$((fail + 1))
  failed_tests+=("tests/python/test_skill_structure.py")
fi

if [ "$list_only" -eq 1 ]; then
  exit 0
fi

echo
echo "===== $pass passed, $skip skipped, $fail failed ====="
if [ "$fail" -gt 0 ]; then
  printf 'failed:\n'
  printf '  %s\n' "${failed_tests[@]}"
fi
exit $fail
