#!/usr/bin/env bash
# Smoke + lifecycle test for scripts/watch-dispatch-tail.sh.
#
# Covers all four exit conditions:
#   - exit 0: terminal marker appears in tail (canonical happy path)
#   - exit 1: worker PID dies without any terminal marker
#   - exit 2: idle timeout (no tail update for max-idle-secs)
#   - exit 3: orchestrator PID dies (orphan watcher self-detection)
#
# And pidfile lifecycle:
#   - on startup: per-watcher subfile written under /tmp/goal-flight-acp-pids.d/
#   - on clean exit (any code): subfile removed via EXIT trap

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
WATCHER="$REPO_ROOT/scripts/watch-dispatch-tail.sh"
FIXTURE_DIR="$REPO_ROOT/tests/fixtures/watch_prompt_echo"

# Hermetic pidfile dir. The watcher honors $GOAL_FLIGHT_PIDFILE_DIR, so exporting
# it here makes every watcher child register under a private temp dir instead of
# the real production dir (/tmp/goal-flight-acp-pids.d).
#
# Why this matters: case 4 deliberately lets the controller-proxy die, which makes
# its still-alive worker a genuine "orphan". If the test wrote into the shared
# production dir, a concurrent acp_client.cleanup_ghosts() (real goal-flight
# activity, or a sibling test) would legitimately reap that orphan — SIGKILL the
# worker and unlink the pidfile — mid-test. That surfaced as a ~1-in-3 flake on
# "case-4 pidfile preserved" (got=removed), passing on re-run, depending purely on
# whether anything else triggered cleanup_ghosts during the ~2s window. Cases 1/3
# escaped it only because their orchestrator ($$) stays alive, so cleanup_ghosts
# skips them. Isolating the dir removes the shared-state dependency entirely.
PIDFILE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/goal-flight-test-pids.XXXXXX")" || {
  # Refuse to silently fall back to the shared production dir (an empty
  # GOAL_FLIGHT_PIDFILE_DIR would make the watcher use its default) — that would
  # reintroduce the very cross-contamination this isolation prevents.
  echo "FATAL: mktemp -d failed; cannot create isolated pidfile dir" >&2
  exit 1
}
export GOAL_FLIGHT_PIDFILE_DIR="$PIDFILE_DIR"
trap 'rm -rf "$PIDFILE_DIR"' EXIT

pass=0
fail=0
note() { echo "  $*" >&2; }
expect_eq() {
  local label="$1" expected="$2" got="$3"
  if [ "$got" = "$expected" ]; then
    note "PASS  $label  ($got)"
    pass=$((pass + 1))
  else
    echo "FAIL  $label  expected=$expected got=$got" >&2
    fail=$((fail + 1))
  fi
}
cleanup_pidfile() {
  local stem="$1"
  rm -f "$PIDFILE_DIR/$stem"
}
wait_for_file() {
  local path="$1" attempts="${2:-50}" i
  i=0
  while [ "$i" -lt "$attempts" ]; do
    [ -f "$path" ] && return 0
    sleep 0.1
    i=$((i + 1))
  done
  [ -f "$path" ]
}
start_isolated_sleep() {
  local duration="$1"
  python3 - "$duration" <<'PY'
import subprocess
import sys

duration = sys.argv[1]
proc = subprocess.Popen(
    ["sleep", duration],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
)
print(proc.pid)
PY
}
run_dead_tail_case() {
  local label="$1"
  local tail="$2"
  local prompt="$3"
  local out="$4"
  local expected="$5"
  local dispatch_id="${6:-$label}"
  local worker_sleep="5"

  # Negative dead-tail cases need enough startup margin for the watcher to
  # register the worker before it exits; otherwise they fail before emitting the
  # WATCHER-EXIT summary the case is checking.
  [ "$expected" = "1" ] && worker_sleep="2.5"
  sleep "$worker_sleep" & WORKER_PID=$!
  PIDFILE_STEM="$$.bashtail.${WORKER_PID}.jsonl"
  bash "$WATCHER" \
    --pid "$WORKER_PID" --tail "$tail" \
    --controller-pid "$$" --agent test-bashtail \
    --session-id "$dispatch_id" \
    --ignore-prompt-file "$prompt" \
    --poll-secs 1 --max-idle-secs 30 \
    > "$out" 2>&1
  watcher_exit=$?
  kill "$WORKER_PID" 2>/dev/null
  wait "$WORKER_PID" 2>/dev/null
  expect_eq "$label exit code" "$expected" "$watcher_exit"
  cleanup_pidfile "$PIDFILE_STEM"
}

run_pid_dead_grace_marker_case() {
  local label="$1"
  local tail="$2"
  local prompt="$3"
  local out="$4"
  local marker="$5"
  local expected="$6"
  local dispatch_id="${7:-$label}"

  : > "$tail"
  (
    sleep 2.5
    {
      echo "grok worker completed review"
      echo "$marker"
    } >> "$tail"
  ) & WORKER_PID=$!
  PIDFILE_STEM="$$.bashtail.${WORKER_PID}.jsonl"
  bash "$WATCHER" \
    --pid "$WORKER_PID" --tail "$tail" \
    --controller-pid "$$" --agent test-bashtail \
    --session-id "$dispatch_id" \
    --ignore-prompt-file "$prompt" \
    --poll-secs 1 --max-idle-secs 30 \
    > "$out" 2>&1
  watcher_exit=$?
  wait "$WORKER_PID" 2>/dev/null
  expect_eq "$label exit code" "$expected" "$watcher_exit"
  cleanup_pidfile "$PIDFILE_STEM"
}

# Spawn most workers directly in the test shell (NOT in a subshell via
# `$(spawn_fn)` — that kills ordinary background children when the subshell
# exits). `start_isolated_sleep` is the exception: Python starts a new session
# and prints the orphaned sleep PID so same-PGID test harness CPU cannot mask a
# quiet fake worker as running_quiet.

# ---- Case 1: live growth vetoes candidate; worker exit reconciles it → exit 0 ----
TAIL=/tmp/test-watch-marker-$$.txt
: > "$TAIL"
TRIGGER=/tmp/test-watch-marker-trigger-$$
(
  while [ ! -f "$TRIGGER" ]; do sleep 0.05; done
  echo "**COMPLETE:** test-marker — test fixture candidate"
  sleep 1.5
  echo "worker output continued after the candidate"
  sleep 2
) >> "$TAIL" & WORKER_PID=$!
SLUG="test-marker"
PIDFILE_STEM="$$.bashtail.${WORKER_PID}.jsonl"

# Start watcher in background with aggressive poll for quick test cycle.
bash "$WATCHER" \
  --pid "$WORKER_PID" --tail "$TAIL" \
  --controller-pid "$$" --agent test-bashtail \
  --session-id "$SLUG" \
  --poll-secs 1 --max-idle-secs 30 \
  > /tmp/watcher-out-marker-$$.txt 2>&1 &
WATCHER_PID=$!

# Verify pidfile exists
if wait_for_file "$PIDFILE_DIR/$PIDFILE_STEM" 50; then
  expect_eq "case-1 pidfile written at startup" "yes" "yes"
else
  expect_eq "case-1 pidfile written at startup" "yes" "no"
fi

# Let the worker expose a marker as its last line, then grow the tail while it
# remains alive. The watcher must discard that candidate instead of exiting 0.
: > "$TRIGGER"
sleep 2.3
watcher_finished_early=0
watcher_exit=0
if kill -0 "$WATCHER_PID" 2>/dev/null; then
  expect_eq "case-1 live growth keeps watcher running" "running" "running"
else
  wait "$WATCHER_PID"
  watcher_exit=$?
  watcher_finished_early=1
  expect_eq "case-1 live growth keeps watcher running" "running" "exited-$watcher_exit"
fi

# The same historical marker becomes legitimate terminal evidence once the
# worker exits. Death reconciliation must then return the normal success code.
wait "$WORKER_PID" 2>/dev/null
if [ "$watcher_finished_early" -eq 0 ]; then
  wait "$WATCHER_PID"
  watcher_exit=$?
fi
expect_eq "case-1 marker then worker exit" "0" "$watcher_exit"
if [ -f "$PIDFILE_DIR/$PIDFILE_STEM" ]; then
  expect_eq "case-1 pidfile removed after worker exit" "removed" "preserved"
else
  expect_eq "case-1 pidfile removed after worker exit" "removed" "removed"
fi
if grep -q "WATCHER-DISCARD: terminal candidate disproved by live tail growth" /tmp/watcher-out-marker-$$.txt; then
  expect_eq "case-1 growth veto recorded" "yes" "yes"
else
  expect_eq "case-1 growth veto recorded" "yes" "no"
fi
if grep -q "WATCHER-EXIT: marker exit_code=0" /tmp/watcher-out-marker-$$.txt; then
  expect_eq "case-1 WATCHER-EXIT summary line emitted" "yes" "yes"
else
  expect_eq "case-1 WATCHER-EXIT summary line emitted" "yes" "no"
fi
rm -f "$TAIL" "$TRIGGER" /tmp/watcher-out-marker-$$.txt
cleanup_pidfile "$PIDFILE_STEM"
if [ "${GOALFLIGHT_TEST_CASE1_ONLY:-0}" = "1" ]; then
  echo "===== watch-dispatch-tail case-1: $pass passed, $fail failed ====="
  exit "$fail"
fi

# ---- Case 1q: a quiet live success candidate stays pending through grace ----
# The six-second candidate grace is only a growth-veto window. A live worker
# that remains quiet past it is still live evidence, so the watcher must keep
# watching until worker exit (or the ordinary max-idle classifier).
TAIL=/tmp/test-watch-quiet-live-marker-$$.txt
OUT=/tmp/watcher-out-quiet-live-marker-$$.txt
: > "$TAIL"
(
  echo "**COMPLETE:** quiet-live-marker — bounded work complete"
  sleep 8
) >> "$TAIL" & WORKER_PID=$!
PIDFILE_STEM="$$.bashtail.${WORKER_PID}.jsonl"
bash "$WATCHER" \
  --pid "$WORKER_PID" --tail "$TAIL" \
  --controller-pid "$$" --agent test-bashtail \
  --session-id quiet-live-marker \
  --poll-secs 1 --max-idle-secs 30 \
  > "$OUT" 2>&1 &
WATCHER_PID=$!

# Wait past POST_TERMINAL_EXIT_GRACE_SECS (6s) while the worker remains alive.
sleep 7
quiet_watcher_finished_early=0
watcher_exit=0
if kill -0 "$WATCHER_PID" 2>/dev/null; then
  expect_eq "case-1q quiet live marker stays pending through grace" "running" "running"
else
  wait "$WATCHER_PID"
  watcher_exit=$?
  quiet_watcher_finished_early=1
  expect_eq "case-1q quiet live marker stays pending through grace" "running" "exited-$watcher_exit"
fi
wait "$WORKER_PID" 2>/dev/null
if [ "$quiet_watcher_finished_early" -eq 0 ]; then
  wait "$WATCHER_PID"
  watcher_exit=$?
fi
expect_eq "case-1q worker exit reconciles quiet marker" "0" "$watcher_exit"
if grep -q "terminal candidate still pending while worker is alive" "$OUT"; then
  expect_eq "case-1q pending state recorded" "yes" "yes"
else
  expect_eq "case-1q pending state recorded" "yes" "no"
fi
rm -f "$TAIL" "$OUT"
cleanup_pidfile "$PIDFILE_STEM"

# ---- Case 1r: tail growth resets the outer event-silence clock ----
TAIL=/tmp/test-watch-runtime-bound-$$.txt
OUT=/tmp/watcher-out-runtime-bound-$$.txt
CHILD_PID_FILE=/tmp/test-watch-runtime-child-$$.txt
: > "$TAIL"
(
  sleep 60 &
  child_pid=$!
  echo "$child_pid" > "$CHILD_PID_FILE"
  echo "STATUS: runtime-bound initial"
  sleep 2
  echo "STATUS: runtime-bound reset"
  wait "$child_pid"
) >> "$TAIL" & WORKER_PID=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [ -s "$CHILD_PID_FILE" ] && break
  sleep 0.1
done
expect_eq "case-1r live descendant precondition" "yes" "$([ -s "$CHILD_PID_FILE" ] && kill -0 "$(cat "$CHILD_PID_FILE")" 2>/dev/null && echo yes || echo no)"
PIDFILE_STEM="$$.bashtail.${WORKER_PID}.jsonl"
case_1r_started=$(date +%s)
GOALFLIGHT_WATCH_TOTAL_RUNTIME_SECS=3 bash "$WATCHER" \
  --pid "$WORKER_PID" --tail "$TAIL" \
  --controller-pid "$$" --agent test-bashtail \
  --session-id runtime-bound \
  --poll-secs 1 --max-idle-secs 1 \
  > "$OUT" 2>&1
watcher_exit=$?
case_1r_elapsed=$(( $(date +%s) - case_1r_started ))
expect_eq "case-1r event-silence exit code" "2" "$watcher_exit"
if [ "$case_1r_elapsed" -ge 4 ]; then
  expect_eq "case-1r tail growth reset the outer clock" "yes" "yes"
else
  expect_eq "case-1r tail growth reset the outer clock" "elapsed-at-least-4" "$case_1r_elapsed"
fi
if grep -q "WATCHER-EXIT: liveness_indeterminate exit_code=2" "$OUT"; then
  expect_eq "case-1r event-silence summary" "yes" "yes"
else
  expect_eq "case-1r event-silence summary" "yes" "no"
fi
kill "$WORKER_PID" 2>/dev/null
wait "$WORKER_PID" 2>/dev/null
rm -f "$TAIL" "$OUT" "$CHILD_PID_FILE"
cleanup_pidfile "$PIDFILE_STEM"

# ---- Case 1s: real descendant-probe failure gives up as indeterminate ----
TAIL=/tmp/test-watch-unknown-bound-$$.txt
OUT=/tmp/watcher-out-unknown-bound-$$.txt
: > "$TAIL"
PS_FAIL_DIR="$PIDFILE_DIR/ps-fail"
mkdir -p "$PS_FAIL_DIR"
REAL_PS=$(command -v ps)
cat > "$PS_FAIL_DIR/ps" <<EOF
#!/bin/sh
case " \$* " in
  *" pid=,ppid="*)
    echo 'ps: enumeration failed' >&2
    exit 1
    ;;
  *" pgid=,pid=,time= "*)
    echo "\${GOALFLIGHT_TEST_WORKER_PID:-0} \${GOALFLIGHT_TEST_WORKER_PID:-0} 0:00.00"
    exit 0
    ;;
  *" pgid= "*)
    echo "\${GOALFLIGHT_TEST_WORKER_PID:-0}"
    exit 0
    ;;
esac
exec "$REAL_PS" "\$@"
EOF
chmod +x "$PS_FAIL_DIR/ps"
PATH="$PS_FAIL_DIR:$PATH" ps -axo pid=,ppid=,state= >/dev/null 2>&1
expect_eq "case-1s descendant probe really fails" "1" "$?"
WORKER_PID=$(start_isolated_sleep 60)
PIDFILE_STEM="$$.bashtail.${WORKER_PID}.jsonl"
PATH="$PS_FAIL_DIR:$PATH" GOALFLIGHT_TEST_WORKER_PID="$WORKER_PID" \
  GOALFLIGHT_WATCH_TOTAL_RUNTIME_SECS=3 bash "$WATCHER" \
  --pid "$WORKER_PID" --tail "$TAIL" \
  --controller-pid "$$" --agent test-bashtail \
  --session-id unknown-bound \
  --poll-secs 1 --max-idle-secs 1 \
  > "$OUT" 2>&1
watcher_exit=$?
expect_eq "case-1s unknown-probe outer-bound exit code" "2" "$watcher_exit"
if grep -q "live_descendants=unknown" "$OUT"; then
  expect_eq "case-1s real unknown descendant observed" "yes" "yes"
else
  expect_eq "case-1s real unknown descendant observed" "yes" "no"
fi
if grep -q "WATCHER-EXIT: liveness_indeterminate exit_code=2" "$OUT"; then
  expect_eq "case-1s unknown reason vocabulary" "yes" "yes"
else
  expect_eq "case-1s unknown reason vocabulary" "yes" "no"
fi
if grep -q "WATCHER-EXIT: idle-timeout" "$OUT"; then
  expect_eq "case-1s unknown is never idle" "absent" "present"
else
  expect_eq "case-1s unknown is never idle" "absent" "absent"
fi
kill "$WORKER_PID" 2>/dev/null
wait "$WORKER_PID" 2>/dev/null
rm -f "$TAIL" "$OUT"
cleanup_pidfile "$PIDFILE_STEM"

# ---- Case 1t: confirmed idle outranks a coincident outer bound ----
TAIL=/tmp/test-watch-known-idle-bound-$$.txt
OUT=/tmp/watcher-out-known-idle-bound-$$.txt
: > "$TAIL"
PS_ZERO_DIR="$PIDFILE_DIR/ps-zero"
mkdir -p "$PS_ZERO_DIR"
cat > "$PS_ZERO_DIR/ps" <<'EOF'
#!/bin/sh
case " $* " in
  *" pid=,ppid="*)
    echo "${GOALFLIGHT_TEST_WORKER_PID:-0} 1 S"
    exit 0
    ;;
  *" pgid=,pid=,time= "*)
    echo "${GOALFLIGHT_TEST_WORKER_PID:-0} ${GOALFLIGHT_TEST_WORKER_PID:-0} 0:00.00"
    exit 0
    ;;
  *" pgid= "*)
    echo "${GOALFLIGHT_TEST_WORKER_PID:-0}"
    exit 0
    ;;
esac
exit 1
EOF
chmod +x "$PS_ZERO_DIR/ps"
WORKER_PID=$(start_isolated_sleep 60)
PIDFILE_STEM="$$.bashtail.${WORKER_PID}.jsonl"
known_idle_rows=$(PATH="$PS_ZERO_DIR:$PATH" GOALFLIGHT_TEST_WORKER_PID="$WORKER_PID" ps -axo pid=,ppid=,state=)
expect_eq "case-1t measured-zero descendant precondition" "$WORKER_PID 1 S" "$known_idle_rows"
PATH="$PS_ZERO_DIR:$PATH" GOALFLIGHT_TEST_WORKER_PID="$WORKER_PID" \
  GOALFLIGHT_WATCH_TOTAL_RUNTIME_SECS=2 bash "$WATCHER" \
  --pid "$WORKER_PID" --tail "$TAIL" \
  --controller-pid "$$" --agent test-bashtail \
  --session-id known-idle-bound \
  --poll-secs 1 --max-idle-secs 1 \
  > "$OUT" 2>&1
watcher_exit=$?
expect_eq "case-1t confirmed idle exit code" "2" "$watcher_exit"
if grep -q "WATCHER-EXIT: idle-timeout exit_code=2" "$OUT"; then
  expect_eq "case-1t confirmed idle beats indeterminate" "yes" "yes"
else
  expect_eq "case-1t confirmed idle beats indeterminate" "yes" "no"
fi
if grep -q "WATCHER-EXIT: liveness_indeterminate" "$OUT"; then
  expect_eq "case-1t no false indeterminate" "absent" "present"
else
  expect_eq "case-1t no false indeterminate" "absent" "absent"
fi
kill "$WORKER_PID" 2>/dev/null
wait "$WORKER_PID" 2>/dev/null
rm -f "$TAIL" "$OUT"
cleanup_pidfile "$PIDFILE_STEM"

# ---- Case 1a: custom regex remains dispatch-bound ----
# A legacy --markers override may further filter a valid marker, but cannot
# bypass the shared parser's expected-dispatch identity check.
TAIL=/tmp/test-watch-custom-marker-$$.txt
OUT=/tmp/watcher-out-custom-marker-$$.txt
: > "$TAIL"
sleep 30 & WORKER_PID=$!
PIDFILE_STEM="$$.bashtail.${WORKER_PID}.jsonl"
bash "$WATCHER" \
  --pid "$WORKER_PID" --tail "$TAIL" \
  --controller-pid "$$" --agent test-bashtail \
  --session-id "custom-marker" --markers 'COMPLETE:' \
  --poll-secs 1 --max-idle-secs 30 \
  > "$OUT" 2>&1 &
WATCHER_PID=$!
wait_for_file "$PIDFILE_DIR/$PIDFILE_STEM" 50
echo "COMPLETE: foreign-marker" >> "$TAIL"
sleep 2
if kill -0 "$WATCHER_PID" 2>/dev/null; then
  expect_eq "case-1a custom regex rejects foreign dispatch" "running" "running"
else
  wait "$WATCHER_PID"
  expect_eq "case-1a custom regex rejects foreign dispatch" "running" "exited-$?"
fi
echo "COMPLETE: custom-marker — done" >> "$TAIL"
wait "$WATCHER_PID"
expect_eq "case-1a custom regex accepts expected dispatch" "0" "$?"
kill "$WORKER_PID" 2>/dev/null
wait "$WORKER_PID" 2>/dev/null
rm -f "$TAIL" "$OUT"
cleanup_pidfile "$PIDFILE_STEM"

# ---- Case 1b: marker received + worker also dead → pidfile REMOVED ----
# Same as case 1 but the worker exits before the watcher exits, so the trap
# removes the pidfile.
TAIL=/tmp/test-watch-marker-dead-$$.txt
: > "$TAIL"
sleep 2 & WORKER_PID=$!
PIDFILE_STEM="$$.bashtail.${WORKER_PID}.jsonl"

bash "$WATCHER" \
  --pid "$WORKER_PID" --tail "$TAIL" \
  --controller-pid "$$" --agent test-bashtail \
  --session-id "test-marker-dead" \
  --poll-secs 1 --max-idle-secs 30 \
  > /tmp/watcher-out-marker-dead-$$.txt 2>&1 &
WATCHER_PID=$!
wait "$WORKER_PID" 2>/dev/null
sleep 0.1
echo "**COMPLETE:** test-marker-dead — done" >> "$TAIL"
sleep 0.5  # let watcher observe marker after worker exit
wait "$WATCHER_PID"
watcher_exit=$?
expect_eq "case-1b exit code (marker + worker dead)" "0" "$watcher_exit"
if [ -f "$PIDFILE_DIR/$PIDFILE_STEM" ]; then
  expect_eq "case-1b pidfile removed (worker dead on exit)" "removed" "still-present"
else
  expect_eq "case-1b pidfile removed (worker dead on exit)" "removed" "removed"
fi
rm -f "$TAIL" /tmp/watcher-out-marker-dead-$$.txt
cleanup_pidfile "$PIDFILE_STEM"

# ---- Case 1c: worker-dead final reconciliation sees bare COMPLETE before trailing prose ----
TAIL=/tmp/test-watch-dead-reconcile-pynec-$$.txt
PROMPT=/tmp/test-watch-dead-reconcile-pynec-$$.prompt
OUT=/tmp/watcher-out-dead-reconcile-pynec-$$.txt
: > "$PROMPT"
cat > "$TAIL" <<'EOF'

RESULT: W-pynec-fixes-2
- `short_dipole`: max_gain_dbi `1.7496324917822492`, directivity/gain linear `1.4961090471558036`
- `half_wave_dipole`: max_gain_dbi `2.17743874914555`, directivity/gain linear `1.6509878413064911`
- `small_loop_screen`: max_gain_dbi `1.7429620750016896`, gain linear `1.4938129068081143`
- Grading remains honestly `BLOCKED(pynec-source-unresolved)` / `REPORT_ONLY`; no literature numbers fabricated.

COMPLETE: W-pynec-fixes-2

No commit made. `GOALFLIGHT_STEER_FILE` was unset in this process, so no steer ack was possible.

EOF
run_dead_tail_case "case-1c dead reconcile bare COMPLETE" "$TAIL" "$PROMPT" "$OUT" "0" "W-pynec-fixes-2"
if grep -q "terminal marker reconciled after worker exit" "$OUT"; then
  expect_eq "case-1c reconciliation summary emitted" "yes" "yes"
else
  expect_eq "case-1c reconciliation summary emitted" "yes" "no"
fi
rm -f "$TAIL" "$PROMPT" "$OUT"

# ---- Case 1d: worker-dead final reconciliation sees STATUS: COMPLETE well before final prose ----
TAIL=/tmp/test-watch-dead-reconcile-rf-$$.txt
PROMPT=/tmp/test-watch-dead-reconcile-rf-$$.prompt
OUT=/tmp/watcher-out-dead-reconcile-rf-$$.txt
: > "$PROMPT"
{
  echo "STATUS: COMPLETE: W-rf-b5-round5"
  for idx in 1 2 3 4 5 6 7 8 9 10 11 12; do
    echo "post-marker summary line $idx"
  done
  cat <<'EOF'
- [live-grade-2026-06-11-round5.md](/Users/simonrowland/Repos/kiln/docs-private/research/2026-06-11-battery-blast/rf-b5/live-grade-2026-06-11-round5.md)

Verification:
- `PYTHONPATH=$PWD:$HOME/Repos python3 -m pytest templates/tests/test_analytic_plasma_decks.py -q`
- `48 passed, 11 skipped`
- `git diff --check` clean

Production controller should run production RF-B5 variants: base, half-ne, double-ne, double-b, flip-b, vacuum, then grade with `grade_rf_faraday_openpmd` in an environment with `h5py`.

FARR/PyNEC files were not touched; FARR P1 must align to this family Faraday sign convention in follow-up.

EOF
} > "$TAIL"
run_dead_tail_case "case-1d dead reconcile STATUS COMPLETE" "$TAIL" "$PROMPT" "$OUT" "0" "W-rf-b5-round5"
rm -f "$TAIL" "$PROMPT" "$OUT"

# ---- Case 1e: worker-dead final reconciliation sees markdown bullet/backtick COMPLETE ----
TAIL=/tmp/test-watch-dead-reconcile-synchrad-$$.txt
PROMPT=/tmp/test-watch-dead-reconcile-synchrad-$$.prompt
OUT=/tmp/watcher-out-dead-reconcile-synchrad-$$.txt
: > "$PROMPT"
cat > "$TAIL" <<'EOF'
- Run-spec env coverage.
- No-device fail-closed test without real `pyopencl`.

Verification:
- `PYTHONPATH=$PWD:$HOME/Repos python3 -m pytest templates/tests/test_rf_synchrad_larmor.py -q` -> `12 passed in 0.75s`
- `git diff --check` clean for target files.
- `RESULT: W-synchrad-ctx pytest exit=0`
- `COMPLETE: W-synchrad-ctx tests`

No live SynchRad run. No commit. `$GOALFLIGHT_STEER_FILE` was unset in tool env, so no steer messages to ack.

EOF
run_dead_tail_case "case-1e dead reconcile bullet backtick COMPLETE" "$TAIL" "$PROMPT" "$OUT" "0" "W-synchrad-ctx"
rm -f "$TAIL" "$PROMPT" "$OUT"

# ---- Case 1f: diff-context marker echo is not a terminal sign-off ----
TAIL=/tmp/test-watch-dead-reconcile-diff-$$.txt
PROMPT=/tmp/test-watch-dead-reconcile-diff-$$.prompt
OUT=/tmp/watcher-out-dead-reconcile-diff-$$.txt
: > "$PROMPT"
cat > "$TAIL" <<'EOF'
diff --git a/file b/file
@@ -1 +1 @@
+STATUS: COMPLETE: diff-output-only
worker died before sign-off
EOF
run_dead_tail_case "case-1f dead reconcile rejects diff echo" "$TAIL" "$PROMPT" "$OUT" "1"
if grep -q "WATCHER-EXIT: pid-dead exit_code=1" "$OUT"; then
  expect_eq "case-1f pid-dead summary emitted" "yes" "yes"
else
  expect_eq "case-1f pid-dead summary emitted" "yes" "no"
fi
rm -f "$TAIL" "$PROMPT" "$OUT"

# ---- Case 1g: prompt-echo-only marker is ignored on worker-dead reconciliation ----
TAIL=/tmp/test-watch-dead-reconcile-prompt-$$.txt
PROMPT=/tmp/test-watch-dead-reconcile-prompt-$$.prompt
OUT=/tmp/watcher-out-dead-reconcile-prompt-$$.txt
cat > "$PROMPT" <<'EOF'
Do the work.
COMPLETE: prompt-only
EOF
{
  cat "$PROMPT"
  echo "worker died before sign-off"
} > "$TAIL"
run_dead_tail_case "case-1g dead reconcile rejects prompt echo" "$TAIL" "$PROMPT" "$OUT" "1"
rm -f "$TAIL" "$PROMPT" "$OUT"

# ---- Case 1g1: codex banner before prompt echo is still fenced on worker-dead reconciliation ----
TAIL=/tmp/test-watch-dead-reconcile-banner-offset-$$.txt
PROMPT=/tmp/test-watch-dead-reconcile-banner-offset-$$.prompt
OUT=/tmp/watcher-out-dead-reconcile-banner-offset-$$.txt
cat > "$PROMPT" <<'EOF'
Do the watcher reconciliation.
The final line must be exactly:
COMPLETE: gf-fence-offset-fix
or BLOCKED: reason.
EOF
{
  cat <<'EOF'
OpenAI Codex v0.137.0
--------
workdir: /Users/simonrowland/Repos/goal-flight
model: gpt-5.5
provider: openai
approval: never
sandbox: workspace-write [workdir, /tmp, $TMPDIR]
reasoning effort: xhigh
reasoning summaries: none
session id: 019eb974-0dee-79d2-b315-8d2910167bf4
--------
user
You have a steer mailbox at `$GOALFLIGHT_STEER_FILE`. Read it AT THE TOP OF EACH ITERATION and IMMEDIATELY BEFORE ANY git commit/push. Incorporate new messages into your plan; ack each with `STEER-ACK

EOF
  cat "$PROMPT"
  cat <<'EOF'
worker started
mcp: context-mode/ctx_execute started
EOF
} > "$TAIL"
run_dead_tail_case "case-1g1 dead reconcile rejects banner-offset prompt echo" "$TAIL" "$PROMPT" "$OUT" "1"
rm -f "$TAIL" "$PROMPT" "$OUT"

# ---- Case 1g2: codex banner + echo + genuine bare COMPLETE after work reconciles ----
TAIL=/tmp/test-watch-dead-reconcile-banner-genuine-$$.txt
PROMPT=/tmp/test-watch-dead-reconcile-banner-genuine-$$.prompt
OUT=/tmp/watcher-out-dead-reconcile-banner-genuine-$$.txt
cat > "$PROMPT" <<'EOF'
Do the watcher reconciliation.
The final line must be exactly:
COMPLETE: gf-fence-offset-fix
or BLOCKED: reason.
EOF
{
  cat <<'EOF'
OpenAI Codex v0.137.0
--------
workdir: /Users/simonrowland/Repos/goal-flight
model: gpt-5.5
provider: openai
approval: never
sandbox: workspace-write [workdir, /tmp, $TMPDIR]
reasoning effort: xhigh
reasoning summaries: none
session id: 019eb974-0dee-79d2-b315-8d2910167bf4
--------
user
You have a steer mailbox at `$GOALFLIGHT_STEER_FILE`. Read it AT THE TOP OF EACH ITERATION and IMMEDIATELY BEFORE ANY git commit/push. Incorporate new messages into your plan; ack each with `STEER-ACK

EOF
  cat "$PROMPT"
  cat <<'EOF'
worker finished real work
COMPLETE: gf-fence-offset-fix
post-marker summary
EOF
} > "$TAIL"
run_dead_tail_case "case-1g2 dead reconcile accepts banner-offset genuine COMPLETE" "$TAIL" "$PROMPT" "$OUT" "0" "gf-fence-offset-fix"
rm -f "$TAIL" "$PROMPT" "$OUT"

# ---- Case 1g3: pid-dead grace accepts genuine final marker even when prompt quotes it ----
TAIL=/tmp/test-watch-dead-grace-fenceless-final-$$.txt
PROMPT=/tmp/test-watch-dead-grace-fenceless-final-$$.prompt
OUT=/tmp/watcher-out-dead-grace-fenceless-final-$$.txt
cat > "$PROMPT" <<'EOF'
Do the watcher reconciliation.
Final line of your output MUST be exactly:
COMPLETE: gf-fence-offset-fix-r2
EOF
run_pid_dead_grace_marker_case "case-1g3 pid-dead grace accepts fenceless final COMPLETE" "$TAIL" "$PROMPT" "$OUT" "COMPLETE: gf-fence-offset-fix-r2" "0" "gf-fence-offset-fix-r2"
rm -f "$TAIL" "$PROMPT" "$OUT"

# ---- Case 1g4: early narration equal to prompt line 1 does not leave the real echo unfenced ----
TAIL=/tmp/test-watch-dead-reconcile-early-latch-$$.txt
PROMPT=/tmp/test-watch-dead-reconcile-early-latch-$$.prompt
OUT=/tmp/watcher-out-dead-reconcile-early-latch-$$.txt
cat > "$PROMPT" <<'EOF'
Do the watcher reconciliation.
The final line must be exactly:
COMPLETE: gf-fence-offset-fix-r2
or BLOCKED: reason.
EOF
{
  echo "Do the watcher reconciliation."
  echo "narration line happens to match prompt line one, but this is not the prompt echo"
  cat "$PROMPT"
  echo "worker died before sign-off"
} > "$TAIL"
run_dead_tail_case "case-1g4 dead reconcile retries second prompt anchor" "$TAIL" "$PROMPT" "$OUT" "1"
rm -f "$TAIL" "$PROMPT" "$OUT"

# ---- Case 1g5: fence-less prompt-quoted bare marker stays fail-safe, decorated marker reconciles ----
PROMPT=/tmp/test-watch-dead-reconcile-fenceless-$$.prompt
cat > "$PROMPT" <<'EOF'
Do the watcher reconciliation.
COMPLETE: quoted-only
EOF

TAIL=/tmp/test-watch-dead-reconcile-fenceless-bare-$$.txt
OUT=/tmp/watcher-out-dead-reconcile-fenceless-bare-$$.txt
cat > "$TAIL" <<'EOF'
tail window starts after the prompt anchor
COMPLETE: quoted-only
worker died before sign-off
EOF
run_dead_tail_case "case-1g5 dead reconcile rejects fenceless prompt quote" "$TAIL" "$PROMPT" "$OUT" "1"
rm -f "$TAIL" "$OUT"

TAIL=/tmp/test-watch-dead-reconcile-fenceless-decorated-$$.txt
OUT=/tmp/watcher-out-dead-reconcile-fenceless-decorated-$$.txt
cat > "$TAIL" <<'EOF'
tail window starts after the prompt anchor
STATUS: COMPLETE: quoted-only
worker died after decorated sign-off
EOF
	run_dead_tail_case "case-1g6 dead reconcile accepts fenceless decorated marker" "$TAIL" "$PROMPT" "$OUT" "0" "quoted-only"
	rm -f "$TAIL" "$PROMPT" "$OUT"

	# ---- Case 1g7: steer wrapper prompt + brief-only echo is still recognized as prompt echo ----
	PROMPT=/tmp/test-watch-dead-reconcile-brief-only-$$.prompt
	TAIL=/tmp/test-watch-dead-reconcile-brief-only-$$.txt
	OUT=/tmp/watcher-out-dead-reconcile-brief-only-$$.txt
	cat > "$PROMPT" <<'EOF'
You have a steer mailbox at `$GOALFLIGHT_STEER_FILE`.

Do the watcher reconciliation.
Final line of your output MUST be exactly:
COMPLETE: wrapped-brief-only
or BLOCKED: reason.
EOF
	cat > "$TAIL" <<'EOF'
OpenAI Codex v0.137.0
--------
user
Do the watcher reconciliation.
Final line of your output MUST be exactly:
COMPLETE: wrapped-brief-only
or BLOCKED: reason.
worker died before sign-off
EOF
	run_dead_tail_case "case-1g7 dead reconcile rejects brief-only prompt echo" "$TAIL" "$PROMPT" "$OUT" "1"
	rm -f "$TAIL" "$PROMPT" "$OUT"

	# ---- Case 1g7a: public round-4 trimmed fixture exercises wrapper echo + unbalanced fence ----
	PROMPT="$FIXTURE_DIR/round4-trimmed-assembled.prompt"
	TAIL="$FIXTURE_DIR/round4-trimmed-tail.txt"
	OUT=/tmp/watcher-out-dead-reconcile-public-round4-$$.txt
	run_dead_tail_case "case-1g7a public round4 trimmed fixture final COMPLETE" "$TAIL" "$PROMPT" "$OUT" "0" "public-watch-round4"
	if grep -q "WATCHER-EXIT: marker exit_code=0" "$OUT"; then
	  expect_eq "case-1g7a marker summary emitted" "yes" "yes"
	else
	  expect_eq "case-1g7a marker summary emitted" "yes" "no"
	fi
	rm -f "$OUT"

	# ---- Case 1g8: unbalanced fence-like line cannot hide a genuine final marker ----
	PROMPT=/tmp/test-watch-dead-reconcile-unbalanced-fence-$$.prompt
	TAIL=/tmp/test-watch-dead-reconcile-unbalanced-fence-$$.txt
	OUT=/tmp/watcher-out-dead-reconcile-unbalanced-fence-$$.txt
	: > "$PROMPT"
	cat > "$TAIL" <<'EOF'
work started
    ~~~~^^
traceback underline left the scanner in a fence-like state
COMPLETE: unbalanced-final
EOF
	run_dead_tail_case "case-1g8 dead reconcile accepts unbalanced-fence final COMPLETE" "$TAIL" "$PROMPT" "$OUT" "0" "unbalanced-final"
	rm -f "$TAIL" "$PROMPT" "$OUT"

	# ---- Case 1g9: balanced fenced marker remains suppressed ----
	PROMPT=/tmp/test-watch-dead-reconcile-balanced-fence-$$.prompt
	TAIL=/tmp/test-watch-dead-reconcile-balanced-fence-$$.txt
	OUT=/tmp/watcher-out-dead-reconcile-balanced-fence-$$.txt
	: > "$PROMPT"
	cat > "$TAIL" <<'EOF'
worker quoted an example
```
COMPLETE: fenced-only
```
worker died before sign-off
EOF
	run_dead_tail_case "case-1g9 dead reconcile rejects balanced-fence COMPLETE" "$TAIL" "$PROMPT" "$OUT" "1"
	rm -f "$TAIL" "$PROMPT" "$OUT"

	# ---- Case 1h: diff-ish raw lines are not terminal sign-offs ----
	PROMPT=/tmp/test-watch-dead-reconcile-diff-negative-$$.prompt
: > "$PROMPT"

TAIL=/tmp/test-watch-dead-reconcile-prefixed-status-$$.txt
OUT=/tmp/watcher-out-dead-reconcile-prefixed-status-$$.txt
printf '%s\n' "-STATUS: COMPLETE: x" > "$TAIL"
run_dead_tail_case "case-1h1 dead reconcile accepts prefixed STATUS outside hunk" "$TAIL" "$PROMPT" "$OUT" "0" "x"
if grep -q "WATCHER-EXIT: marker exit_code=0" "$OUT"; then
  expect_eq "case-1h1 prefixed STATUS marker exit summary" "yes" "yes"
else
  expect_eq "case-1h1 prefixed STATUS marker exit summary" "yes" "no"
fi
rm -f "$TAIL" "$OUT"

TAIL=/tmp/test-watch-dead-reconcile-context-line-$$.txt
OUT=/tmp/watcher-out-dead-reconcile-context-line-$$.txt
printf '%s\n' " STATUS: COMPLETE: x" > "$TAIL"
run_dead_tail_case "case-1h2 dead reconcile rejects leading-space context" "$TAIL" "$PROMPT" "$OUT" "1"
if grep -q "WATCHER-EXIT: pid-dead exit_code=1" "$OUT"; then
  expect_eq "case-1h2 worker_dead_no_terminal_marker classification" "yes" "yes"
else
  expect_eq "case-1h2 worker_dead_no_terminal_marker classification" "yes" "no"
fi
rm -f "$TAIL" "$OUT"

TAIL=/tmp/test-watch-dead-reconcile-hunk-delete-$$.txt
OUT=/tmp/watcher-out-dead-reconcile-hunk-delete-$$.txt
cat > "$TAIL" <<'EOF'
@@ -1,1 +1,0 @@
-    COMPLETE: x
EOF
run_dead_tail_case "case-1h3 dead reconcile rejects hunk deletion" "$TAIL" "$PROMPT" "$OUT" "1"
if grep -q "WATCHER-EXIT: pid-dead exit_code=1" "$OUT"; then
  expect_eq "case-1h3 worker_dead_no_terminal_marker classification" "yes" "yes"
else
  expect_eq "case-1h3 worker_dead_no_terminal_marker classification" "yes" "no"
fi
rm -f "$TAIL" "$PROMPT" "$OUT"

# ---- Case 1i: worker-dead final reconciliation maps FAILED to blocked exit ----
TAIL=/tmp/test-watch-dead-reconcile-failed-$$.txt
PROMPT=/tmp/test-watch-dead-reconcile-failed-$$.prompt
OUT=/tmp/watcher-out-dead-reconcile-failed-$$.txt
: > "$PROMPT"
printf '%s\n' "FAILED: x" > "$TAIL"
run_dead_tail_case "case-1i dead reconcile FAILED blocks" "$TAIL" "$PROMPT" "$OUT" "4" "x"
if grep -q "WATCHER-EXIT: marker exit_code=4" "$OUT"; then
  expect_eq "case-1i reconciled FAILED exit summary" "yes" "yes"
else
  expect_eq "case-1i reconciled FAILED exit summary" "yes" "no"
fi
rm -f "$TAIL" "$PROMPT" "$OUT"

# ---- Case 1j: worker-dead final reconciliation maps BLOCKED to blocked exit ----
TAIL=/tmp/test-watch-dead-reconcile-blocked-$$.txt
PROMPT=/tmp/test-watch-dead-reconcile-blocked-$$.prompt
OUT=/tmp/watcher-out-dead-reconcile-blocked-$$.txt
: > "$PROMPT"
cat > "$TAIL" <<'EOF'
BLOCKED: x
post-marker summary
EOF
run_dead_tail_case "case-1j dead reconcile BLOCKED blocks" "$TAIL" "$PROMPT" "$OUT" "4" "x"
if grep -q "WATCHER-EXIT: marker exit_code=4" "$OUT"; then
  expect_eq "case-1j reconciled BLOCKED exit summary" "yes" "yes"
else
  expect_eq "case-1j reconciled BLOCKED exit summary" "yes" "no"
fi
rm -f "$TAIL" "$PROMPT" "$OUT"

# ---- Case 2: worker PID dies without marker → exit 1 ----
TAIL=/tmp/test-watch-piddead-$$.txt
: > "$TAIL"
sleep 2 & WORKER_PID=$!
PIDFILE_STEM="$$.bashtail.${WORKER_PID}.jsonl"

bash "$WATCHER" \
  --pid "$WORKER_PID" --tail "$TAIL" \
  --controller-pid "$$" --agent test-bashtail \
  --session-id "test-piddead" \
  --poll-secs 1 --max-idle-secs 30 \
  > /tmp/watcher-out-piddead-$$.txt 2>&1 &
WATCHER_PID=$!
# Worker exits naturally after 2s; watcher should detect and exit 1.
wait "$WATCHER_PID"
watcher_exit=$?
expect_eq "case-2 exit code on pid-dead-no-marker" "1" "$watcher_exit"
if grep -q "WATCHER-EXIT: pid-dead exit_code=1" /tmp/watcher-out-piddead-$$.txt; then
  expect_eq "case-2 WATCHER-EXIT summary line emitted" "yes" "yes"
else
  expect_eq "case-2 WATCHER-EXIT summary line emitted" "yes" "no"
fi
rm -f "$TAIL" /tmp/watcher-out-piddead-$$.txt
cleanup_pidfile "$PIDFILE_STEM"

# ---- Case 3: idle timeout → exit 2 ----
TAIL=/tmp/test-watch-idle-$$.txt
: > "$TAIL"
# Keep the fake wedged worker alive longer than the idle confirmation grace.
WORKER_PID="$(start_isolated_sleep 20)"
PIDFILE_STEM="$$.bashtail.${WORKER_PID}.jsonl"

# Regression guard: a CPU-busy sibling in the test harness PGID must not mask
# the quiet fake worker as running_quiet. The worker owns a separate session.
(while :; do :; done) & CASE3_BURNER_PID=$!

# Aggressive: max-idle-secs=2 so test runs in seconds. Real defaults are 180s.
bash "$WATCHER" \
  --pid "$WORKER_PID" --tail "$TAIL" \
  --controller-pid "$$" --agent test-bashtail \
  --session-id "test-idle" \
  --poll-secs 1 --max-idle-secs 2 \
  > /tmp/watcher-out-idle-$$.txt 2>&1 &
WATCHER_PID=$!
wait "$WATCHER_PID"
watcher_exit=$?
expect_eq "case-3 exit code on idle-timeout" "2" "$watcher_exit"
if grep -q "WATCHER-EXIT: idle-timeout exit_code=2" /tmp/watcher-out-idle-$$.txt; then
  expect_eq "case-3 WATCHER-EXIT summary line emitted" "yes" "yes"
else
  expect_eq "case-3 WATCHER-EXIT summary line emitted" "yes" "no"
fi
# Idle exit: worker is still alive (just wedged). Pidfile must be PRESERVED
# so cleanup_ghosts() reaps the wedged worker on next orchestrator startup.
if [ -f "$PIDFILE_DIR/$PIDFILE_STEM" ]; then
  expect_eq "case-3 pidfile preserved (worker still alive after idle timeout)" "preserved" "preserved"
else
  expect_eq "case-3 pidfile preserved (worker still alive after idle timeout)" "preserved" "removed"
fi
kill "$CASE3_BURNER_PID" 2>/dev/null
wait "$CASE3_BURNER_PID" 2>/dev/null
kill "$WORKER_PID" 2>/dev/null
wait "$WORKER_PID" 2>/dev/null
rm -f "$TAIL" /tmp/watcher-out-idle-$$.txt
cleanup_pidfile "$PIDFILE_STEM"

# ---- Case 3c: a quiet worker with a live child must not idle-timeout ----
TAIL=/tmp/test-watch-idle-child-$$.txt
: > "$TAIL"
WORKER_PID="$(python3 - <<'PY'
import subprocess
import sys

proc = subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import subprocess, time\n"
        "subprocess.Popen(['sleep', '30'])\n"
        "time.sleep(30)\n",
    ],
    start_new_session=True,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
print(proc.pid)
PY
)"
PIDFILE_STEM="$$.bashtail.${WORKER_PID}.jsonl"
bash "$WATCHER" \
  --pid "$WORKER_PID" --tail "$TAIL" \
  --controller-pid "$$" --agent test-bashtail \
  --session-id "test-idle-child" \
  --poll-secs 1 --max-idle-secs 2 \
  > /tmp/watcher-out-idle-child-$$.txt 2>&1 &
WATCHER_PID=$!
sleep 6
if kill -0 "$WATCHER_PID" 2>/dev/null; then
  expect_eq "case-3c watcher still running with live child" "alive" "alive"
  if grep -q "live_descendants=" /tmp/watcher-out-idle-child-$$.txt; then
    expect_eq "case-3c live_descendants logged" "yes" "yes"
  else
    expect_eq "case-3c live_descendants logged" "yes" "no"
  fi
  kill "$WATCHER_PID" 2>/dev/null
  wait "$WATCHER_PID" 2>/dev/null
else
  wait "$WATCHER_PID"
  expect_eq "case-3c watcher still running with live child" "alive" "exited:$?"
fi
kill "$WORKER_PID" 2>/dev/null
wait "$WORKER_PID" 2>/dev/null
rm -f "$TAIL" /tmp/watcher-out-idle-child-$$.txt
cleanup_pidfile "$PIDFILE_STEM"

# ---- Case 3d: a zombie-only descendant is not live work; idle-timeout ----
TAIL=/tmp/test-watch-idle-zombie-$$.txt
ZOMBIE_PID_FILE=/tmp/test-watch-idle-zombie-child-$$.txt
: > "$TAIL"
export GOALFLIGHT_TEST_ZOMBIE_PID_FILE="$ZOMBIE_PID_FILE"
WORKER_PID="$(python3 - <<'PY'
import os
import subprocess
import sys
import time

proc = subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import os, sys, time\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    os._exit(0)\n"
        "print(child, flush=True)\n"
        "time.sleep(30)\n",
    ],
    start_new_session=True,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL,
    text=True,
)
raw = proc.stdout.readline() if proc.stdout is not None else ""
zombie_pid = int(raw.strip())
deadline = time.monotonic() + 5.0
state = ""
while time.monotonic() < deadline:
    probe = subprocess.run(
        ["ps", "-o", "state=", "-p", str(zombie_pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    state = (probe.stdout or "").strip()
    if state[:1] in {"Z", "z"}:
        break
    time.sleep(0.05)
else:
    proc.kill()
    proc.wait(timeout=5)
    raise SystemExit(f"zombie fixture never reached Z: pid={zombie_pid} state={state!r}")
open(os.environ["GOALFLIGHT_TEST_ZOMBIE_PID_FILE"], "w", encoding="utf-8").write(
    str(zombie_pid)
)
print(proc.pid)
PY
)"
PIDFILE_STEM="$$.bashtail.${WORKER_PID}.jsonl"
if [ -s "$ZOMBIE_PID_FILE" ]; then
  zpid=$(cat "$ZOMBIE_PID_FILE")
  zstate=$(ps -o state= -p "$zpid" 2>/dev/null | tr -d ' ')
  case "$zstate" in
    Z*|z*) expect_eq "case-3d real zombie precondition" "Z" "Z" ;;
    *) expect_eq "case-3d real zombie precondition" "Z" "${zstate:-empty}" ;;
  esac
else
  expect_eq "case-3d real zombie precondition" "Z" "missing"
fi
bash "$WATCHER" \
  --pid "$WORKER_PID" --tail "$TAIL" \
  --controller-pid "$$" --agent test-bashtail \
  --session-id "test-idle-zombie" \
  --poll-secs 1 --max-idle-secs 2 \
  > /tmp/watcher-out-idle-zombie-$$.txt 2>&1
watcher_exit=$?
expect_eq "case-3d zombie-only idle-timeout exit code" "2" "$watcher_exit"
if grep -q "WATCHER-EXIT: idle-timeout exit_code=2" /tmp/watcher-out-idle-zombie-$$.txt; then
  expect_eq "case-3d zombie-only takes idle path" "yes" "yes"
else
  expect_eq "case-3d zombie-only takes idle path" "yes" "no"
fi
if grep -q "live child; tail-quiet is not idle" /tmp/watcher-out-idle-zombie-$$.txt; then
  expect_eq "case-3d zombie is not a live-child veto" "absent" "present"
else
  expect_eq "case-3d zombie is not a live-child veto" "absent" "absent"
fi
if grep -q "WATCHER-EXIT: liveness_indeterminate" /tmp/watcher-out-idle-zombie-$$.txt; then
  expect_eq "case-3d zombie is not indeterminate" "absent" "present"
else
  expect_eq "case-3d zombie is not indeterminate" "absent" "absent"
fi
kill "$WORKER_PID" 2>/dev/null
wait "$WORKER_PID" 2>/dev/null
rm -f "$TAIL" "$ZOMBIE_PID_FILE" /tmp/watcher-out-idle-zombie-$$.txt
unset GOALFLIGHT_TEST_ZOMBIE_PID_FILE
cleanup_pidfile "$PIDFILE_STEM"

# ---- Case 3b: CPU-busy silence → running_quiet, not exit 2 ----
TAIL=/tmp/test-watch-running-quiet-$$.txt
: > "$TAIL"
python3 -c 'import time
end = time.time() + 8
x = 0
while time.time() < end:
    x += 1
' & WORKER_PID=$!
PIDFILE_STEM="$$.bashtail.${WORKER_PID}.jsonl"

bash "$WATCHER" \
  --pid "$WORKER_PID" --tail "$TAIL" \
  --controller-pid "$$" --agent test-bashtail \
  --session-id "test-running-quiet" \
  --poll-secs 1 --max-idle-secs 1 \
  > /tmp/watcher-out-running-quiet-$$.txt 2>&1 &
WATCHER_PID=$!
sleep 5
running_quiet_watcher_alive=no
if kill -0 "$WATCHER_PID" 2>/dev/null; then
  running_quiet_watcher_alive=yes
  expect_eq "case-3b watcher still running during CPU-busy silence" "running" "running"
else
  wait "$WATCHER_PID"
  expect_eq "case-3b watcher still running during CPU-busy silence" "running" "exited-$?"
fi
if grep -q "WATCHER-STATE: running_quiet" /tmp/watcher-out-running-quiet-$$.txt; then
  expect_eq "case-3b running_quiet state logged" "yes" "yes"
else
  expect_eq "case-3b running_quiet state logged" "yes" "no"
fi
if [ "$running_quiet_watcher_alive" = "yes" ]; then
  echo "**COMPLETE:** test-running-quiet — busy worker done" >> "$TAIL"
  wait "$WATCHER_PID"
  watcher_exit=$?
  expect_eq "case-3b exit code after terminal marker" "0" "$watcher_exit"
fi
kill "$WORKER_PID" 2>/dev/null
wait "$WORKER_PID" 2>/dev/null
rm -f "$TAIL" /tmp/watcher-out-running-quiet-$$.txt
cleanup_pidfile "$PIDFILE_STEM"

# ---- Case 4: orchestrator PID dies → exit 3 ----
# Spawn a controller-proxy that exits in 2s. The watcher monitors it as its
# --controller-pid. After 2s the proxy dies; watcher should detect and exit 3.
TAIL=/tmp/test-watch-controller-$$.txt
: > "$TAIL"
sleep 30 & WORKER_PID=$!
sleep 2 & CONTROLLER_PROXY=$!
PIDFILE_STEM="${CONTROLLER_PROXY}.bashtail.${WORKER_PID}.jsonl"

bash "$WATCHER" \
  --pid "$WORKER_PID" --tail "$TAIL" \
  --controller-pid "$CONTROLLER_PROXY" --agent test-bashtail \
  --session-id "test-controller-dead" \
  --poll-secs 1 --max-idle-secs 30 \
  > /tmp/watcher-out-controller-$$.txt 2>&1 &
WATCHER_PID=$!
wait "$WATCHER_PID"
watcher_exit=$?
expect_eq "case-4 exit code on controller-dead" "3" "$watcher_exit"
if grep -q "WATCHER-EXIT: controller-dead exit_code=3" /tmp/watcher-out-controller-$$.txt; then
  expect_eq "case-4 WATCHER-EXIT summary line emitted" "yes" "yes"
else
  expect_eq "case-4 WATCHER-EXIT summary line emitted" "yes" "no"
fi
# Controller-dead exit: worker is still alive (orchestrator just died, worker
# unaffected). Pidfile MUST be preserved so cleanup_ghosts() on the next
# orchestrator startup walks it, identity-verifies the worker, and reaps it.
# This is the load-bearing orphan-defense path the codex hardening reviewer
# specifically flagged in pass 3 — removing the pidfile here orphans the
# worker beyond cleanup_ghosts' reach.
if [ -f "$PIDFILE_DIR/$PIDFILE_STEM" ]; then
  expect_eq "case-4 pidfile preserved (worker survives controller death)" "preserved" "preserved"
else
  expect_eq "case-4 pidfile preserved (worker survives controller death)" "preserved" "removed"
fi
kill "$WORKER_PID" 2>/dev/null
wait "$WORKER_PID" 2>/dev/null
rm -f "$TAIL" /tmp/watcher-out-controller-$$.txt
cleanup_pidfile "$PIDFILE_STEM"

# ---- Case 5: argument validation under macOS default bash 3.2 ----
# Runs the watcher with no args / bad args under /bin/bash explicitly to verify
# (a) exit code 64 (EX_USAGE) and (b) no "bad substitution" leak from bash-4-only
# parameter expansion patterns. The original 0.3.1 version had `${var,,}` on the
# missing-arg path which fails on bash 3.2 — this is the regression guard.
err=/tmp/test-watch-arg-validation-$$.err
/bin/bash "$WATCHER" > /dev/null 2> "$err"
expect_eq "case-5a exit code on no args" "64" "$?"
if grep -q "bad substitution" "$err"; then
  expect_eq "case-5a no bash-3.2 bad-substitution leak" "clean" "leaked"
else
  expect_eq "case-5a no bash-3.2 bad-substitution leak" "clean" "clean"
fi
rm -f "$err"

err=/tmp/test-watch-arg-validation-$$.err
/bin/bash "$WATCHER" --pid abc --tail /tmp/x --controller-pid 123 \
  --agent x --session-id x > /dev/null 2> "$err"
expect_eq "case-5b exit code on non-integer --pid" "64" "$?"
if grep -q "invalid --pid 'abc'" "$err"; then
  expect_eq "case-5b helpful error message" "yes" "yes"
else
  expect_eq "case-5b helpful error message" "yes" "no"
fi
rm -f "$err"

err=/tmp/test-watch-arg-validation-$$.err
/bin/bash "$WATCHER" --pid 123 --tail /tmp/x --controller-pid xyz \
  --agent x --session-id x > /dev/null 2> "$err"
expect_eq "case-5c exit code on non-integer --controller-pid" "64" "$?"
rm -f "$err"

# ---- Case 6: pure CPU helpers (cputime delta, not ps %cpu decaying average) ----
# Mirrors tests/python/test_goalflight_liveness.py pure-function pins so a
# revert to summing ps %cpu fails these without needing a full watcher run.
GOALFLIGHT_WATCH_HELPERS_ONLY=1
# shellcheck source=../../scripts/watch-dispatch-tail.sh
. "$WATCHER"
unset GOALFLIGHT_WATCH_HELPERS_ONLY

# Structural pins: the production snapshot must read cumulative time=, and must
# not request ps's decaying %cpu column on any non-comment line. A revert that
# puts `%cpu=` back into the ps invocation fails both without needing a full
# watcher run (the live spinner case below is the runtime complement).
if grep -q 'pgid=,pid=,time=' "$WATCHER"; then
  expect_eq "case-6 snapshot uses cumulative time= column" "yes" "yes"
else
  expect_eq "case-6 snapshot uses cumulative time= column" "yes" "no"
fi
code_uses_pct_cpu=no
while IFS= read -r line || [ -n "$line" ]; do
  code=${line%%#*}
  case "$code" in
    *%cpu*) code_uses_pct_cpu=yes; break ;;
  esac
done < "$WATCHER"
expect_eq "case-6 no non-comment %cpu in watcher" "no" "$code_uses_pct_cpu"

got=$(parse_ps_cputime "0:00.06")
expect_eq "case-6 parse 0:00.06" "0.060000" "$got"
got=$(parse_ps_cputime "1:02:03")
expect_eq "case-6 parse 1:02:03" "3723.000000" "$got"
got=$(parse_ps_cputime "2-03:04:05")
expect_eq "case-6 parse 2-03:04:05" "183845.000000" "$got"
got=$(parse_ps_cputime "315:47.25")
expect_eq "case-6 parse 315:47.25" "18947.250000" "$got"

got=$(cpu_pct_from_cputime_delta "7 4.0" "7 5.0" 1.0)
expect_eq "case-6 one saturated core is 100%" "100.0" "$got"
got=$(cpu_pct_from_cputime_delta "$(printf '7 4.0\n8 1.0')" "$(printf '7 5.0\n8 2.0')" 1.0)
expect_eq "case-6 two saturated cores is 200%" "200.0" "$got"
got=$(cpu_pct_from_cputime_delta "7 4.0" "7 4.0" 1.0)
expect_eq "case-6 idle process is 0%" "0.0" "$got"
# Exited child must not drive the group negative (pairing pin, not just sign).
got=$(cpu_pct_from_cputime_delta "$(printf '100 10.0\n101 30.0')" "100 10.5" 1.0)
expect_eq "case-6 exited child ignored (not negative)" "50.0" "$got"
# Born child: all of its cpu-time is in-window.
got=$(cpu_pct_from_cputime_delta "100 10.0" "$(printf '100 10.0\n102 2.0')" 2.0)
expect_eq "case-6 born child counts in full" "100.0" "$got"
got=$(cpu_pct_from_cputime_delta "1 0.0" "1 5.0" 0.0)
expect_eq "case-6 nonpositive window is 0%" "0.0" "$got"
got=$(default_total_runtime_secs 180)
expect_eq "case-6 default outer bound survives 55-minute incident" "7200" "$got"
python_floor=$(PYTHONPATH="$REPO_ROOT/scripts" python3 - <<'PY'
from goalflight_liveness import INDETERMINATE_LIVENESS_FLOOR_S
print(int(INDETERMINATE_LIVENESS_FLOOR_S))
PY
)
expect_eq "case-6 shell/Python indeterminate floor parity" "$python_floor" "$got"
got=$(default_total_runtime_secs 8000 15)
expect_eq "case-6 long max-idle leaves confirmation room" "8075" "$got"

# Live sampler: measures NOW, not a decaying average. Spinner in its own
# session (mirrors start_new_session workers), then sleeps — delta must drop
# to ~0 immediately while a decaying %cpu average would still carry the spin.
SPIN_FLAG="/tmp/test-watch-cpu-spin-$$.flag"
rm -f "$SPIN_FLAG"
python3 - "$SPIN_FLAG" <<'PY' &
import os, pathlib, sys, time
os.setsid()
flag = pathlib.Path(sys.argv[1])
end = time.time() + 3.0
x = 0
while time.time() < end:
    x += 1
flag.write_text("done")
time.sleep(60)
PY
SPIN_PID=$!
# Reset any cache left over from earlier helper probes in this shell.
_CPU_CACHE_PGID=""
_CPU_CACHE_TS=""
_CPU_CACHE_SAMPLE=""
# Wait briefly for the child to enter its spin.
sleep 0.3
SPIN_PGID_RAW=$(ps -o pgid= -p "$SPIN_PID" 2>/dev/null)
spin_ps_rc=$?
SPIN_PGID=$(printf '%s' "$SPIN_PGID_RAW" | tr -d ' ')
if [ "$spin_ps_rc" -ne 0 ]; then
  note "SKIP  case-6 live sampler (process inspection denied by test sandbox)"
elif [ -z "$SPIN_PGID" ]; then
  expect_eq "case-6 live spinner started" "yes" "no"
else
  busy=$(pgroup_cpu_pct "$SPIN_PGID")
  # Flat-out spinner on one core must read clearly busy (same floor as python twin).
  if awk -v b="$busy" 'BEGIN { exit !(b + 0 > 25.0) }'; then
    expect_eq "case-6 live spinner reads busy (not decaying-average blind)" "busy" "busy"
  else
    expect_eq "case-6 live spinner reads busy (not decaying-average blind)" "busy>25" "got-$busy"
  fi
  # Wait for the spin→sleep transition the child signals.
  for _i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
    [ -f "$SPIN_FLAG" ] && break
    sleep 0.1
  done
  if [ -f "$SPIN_FLAG" ]; then
    # Discard the mid-spin baseline, then sample a window that lies entirely
    # after the CPU went quiet.
    pgroup_cpu_pct "$SPIN_PGID" >/dev/null
    sleep 0.7
    settled=$(pgroup_cpu_pct "$SPIN_PGID")
    if awk -v s="$settled" 'BEGIN { exit !(s + 0 < 2.0) }'; then
      expect_eq "case-6 stopped spinner reads idle (cputime delta, not decay)" "idle" "idle"
    else
      expect_eq "case-6 stopped spinner reads idle (cputime delta, not decay)" "idle<2" "got-$settled"
    fi
  else
    expect_eq "case-6 spinner signalled finished" "yes" "no"
  fi
fi
kill "$SPIN_PID" 2>/dev/null
wait "$SPIN_PID" 2>/dev/null
rm -f "$SPIN_FLAG"

# ---- Summary ----
echo "===== watch-dispatch-tail: $pass passed, $fail failed ====="
exit $fail
