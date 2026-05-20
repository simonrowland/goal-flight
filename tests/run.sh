#!/usr/bin/env bash
# Discover and run two test suites:
#   - tests/test-*.sh — bash tests (installers, codex overrides, fork-detect)
#   - test/test_*.py — Python tests (ACP client + pool + runner + failure modes)
# One pass/fail per file. Exit code = number of failed tests.
#
# Skips test/dispatch_acp_chunk.py (live e2e against real codex-acp, non-hermetic).

set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

pass=0
fail=0
failed_tests=()
ACP_PY="${GOALFLIGHT_ACP_PYTHON:-$HOME/.goal-flight/venvs/acp-0.10/bin/python}"

# Bash tests (tests/test-*.sh)
cd "$SCRIPT_DIR"
for test in test-*.sh; do
  [ -f "$test" ] || continue
  if bash "$test" > /tmp/goal-flight-test-$$.out 2>&1; then
    echo "PASS  tests/$test"
    pass=$((pass + 1))
  else
    echo "FAIL  tests/$test"
    cat /tmp/goal-flight-test-$$.out | sed 's/^/      /'
    fail=$((fail + 1))
    failed_tests+=("tests/$test")
  fi
  rm -f /tmp/goal-flight-test-$$.out
done

# Python tests (test/test_*.py; skips dispatch_acp_chunk.py — requires live codex-acp)
if command -v python3 >/dev/null 2>&1 && [ -d "$REPO_ROOT/test" ]; then
  cd "$REPO_ROOT"
  for test in test/test_*.py; do
    [ -f "$test" ] || continue
    py="python3"
    case "$test" in
      test/test_acp_*.py)
        py="$ACP_PY"
        ;;
    esac
    if [ "$py" = "$ACP_PY" ] && [ ! -x "$py" ]; then
      echo "FAIL  $test"
      echo "      SDK missing -- run install: $ACP_PY"
      fail=$((fail + 1))
      failed_tests+=("$test")
      continue
    fi
    if "$py" "$test" > /tmp/goal-flight-test-$$.out 2>&1; then
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

echo
echo "===== $pass passed, $fail failed ====="
if [ "$fail" -gt 0 ]; then
  printf 'failed:\n'
  printf '  %s\n' "${failed_tests[@]}"
fi
exit $fail
