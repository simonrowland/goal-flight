#!/usr/bin/env bash
# Discover and run every test-*.sh in this directory. One pass/fail per file.
# Exit code = number of failed tests.

set -u
cd "$(dirname "$0")"

pass=0
fail=0
failed_tests=()

for test in test-*.sh; do
  [ -f "$test" ] || continue
  if bash "$test" > /tmp/goal-flight-test-$$.out 2>&1; then
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

echo
echo "===== $pass passed, $fail failed ====="
if [ "$fail" -gt 0 ]; then
  printf 'failed:\n'
  printf '  %s\n' "${failed_tests[@]}"
fi
exit $fail
