#!/usr/bin/env bash
# Discover and run two test suites:
#   - tests/bash/test-*.sh — bash tests (installers, codex overrides, fork-detect)
#   - tests/python/test_*.py — Python tests (ACP client + pool + runner + failure modes)
# One pass/fail per file. Exit code = number of failed tests.
#
# Skips tests/python/dispatch_acp_chunk.py (live e2e against real codex-acp, non-hermetic).

set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

pass=0
fail=0
failed_tests=()
skill_structure_seen=0
ACP_PY="${GOALFLIGHT_ACP_PYTHON:-$HOME/.goal-flight/venvs/acp-0.10/bin/python}"
list_only=0
if [ "${1:-}" = "--list" ]; then
  list_only=1
fi

run_isolated_test_env() {
  env -u GOALFLIGHT_STEER_FILE -u GOALFLIGHT_ALLOW_EXTERNAL_STEER_FILE "$@"
}

# Bash tests (tests/bash/test-*.sh)
cd "$SCRIPT_DIR/bash"
for test in test-*.sh; do
  [ -f "$test" ] || continue
  if [ "$list_only" -eq 1 ]; then
    echo "tests/bash/$test"
    continue
  fi
  # Live opencode ACP probes are slow (~300s each) and environment-flaky; they can
  # wedge the whole suite on a stalled child. Skip EXECUTION by default; opt in
  # explicitly. Listing above is intentionally unaffected so --list collection
  # stays stable regardless of GOALFLIGHT_LIVE_OPENCODE.
  case "$test" in
    test-opencode-*.sh)
      if [ "${GOALFLIGHT_LIVE_OPENCODE:-0}" != "1" ]; then
        echo "SKIP  tests/bash/$test (live opencode ACP probe; set GOALFLIGHT_LIVE_OPENCODE=1 to run)"
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

# Python tests (tests/python/test_*.py; skips dispatch_acp_chunk.py — requires live codex-acp)
# Golden Master guard: tests/python/test_skill_structure.py is intentionally covered by this glob.
if command -v python3 >/dev/null 2>&1 && [ -d "$REPO_ROOT/tests/python" ]; then
  cd "$REPO_ROOT"
  for test in tests/python/test_*.py; do
    [ -f "$test" ] || continue
    py="python3"
    case "$test" in
      tests/python/test_acp_*.py)
        py="$ACP_PY"
        ;;
    esac
    if [ "$list_only" -ne 1 ] && [ "$py" = "$ACP_PY" ] && [ ! -x "$py" ]; then
      echo "FAIL  $test"
      echo "      SDK missing -- run install: $ACP_PY"
      fail=$((fail + 1))
      failed_tests+=("$test")
      continue
    fi
    if [ "$list_only" -eq 1 ]; then
      echo "$test"
      continue
    fi
    if run_isolated_test_env "$py" "$test" > /tmp/goal-flight-test-$$.out 2>&1; then
      if [ "$test" = "tests/python/test_skill_structure.py" ]; then
        skill_structure_seen=1
      fi
      echo "PASS  $test"
      pass=$((pass + 1))
    else
      if [ "$test" = "tests/python/test_skill_structure.py" ]; then
        skill_structure_seen=1
      fi
      echo "FAIL  $test"
      cat /tmp/goal-flight-test-$$.out | sed 's/^/      /'
      fail=$((fail + 1))
      failed_tests+=("$test")
    fi
    rm -f /tmp/goal-flight-test-$$.out
  done
fi

if [ "$list_only" -ne 1 ] && [ "$skill_structure_seen" -ne 1 ]; then
  echo "FAIL  tests/python/test_skill_structure.py"
  echo "      required Golden Master guard was not executed"
  fail=$((fail + 1))
  failed_tests+=("tests/python/test_skill_structure.py")
fi

if [ "$list_only" -eq 1 ]; then
  exit 0
fi

echo
echo "===== $pass passed, $fail failed ====="
if [ "$fail" -gt 0 ]; then
  printf 'failed:\n'
  printf '  %s\n' "${failed_tests[@]}"
fi
exit $fail
