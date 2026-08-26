#!/usr/bin/env bash
# watch-dispatch-tail.sh — content-aware completion watcher for [bash-tail] dispatches.
#
# Watches a worker's tail file for any TERMINAL marker (COMPLETE / BLOCKED /
# USER-NEED / USER-CONFIRM / READY, with optional markdown emphasis tolerance
# for grok).
# Exits when:
#   - terminal marker reconciled after exit/idle  → exit 0  ("WATCHER-EXIT: marker")
#   - worker PID dies without terminal marker     → exit 1  ("WATCHER-EXIT: pid-dead")
#   - no tail update for --max-idle-secs seconds, no live children, and
#     process-group CPU idle → exit 2  ("WATCHER-EXIT: idle-timeout")
#   - unknown descendants/CPU never count as idle; give-up is runtime-timeout
#   - direct watcher exceeds total runtime         → exit 2  ("WATCHER-EXIT: runtime-timeout")
#   - orchestrator PID dies                         → exit 3  ("WATCHER-EXIT: controller-dead")
#
# Registers a per-watcher entry in the same pidfile dir scripts/acp_client.py uses
# (/tmp/goal-flight-acp-pids.d/), so cleanup_ghosts reaps orphaned workers
# uniformly across ACP and bash-tail dispatch paths.
#
# Filename: <controller-pid>.bashtail.<worker-pid>.jsonl  (one entry per file).
# cleanup_ghosts extracts controller-pid from the leading int prefix.
#
# Usage:
#   watch-dispatch-tail.sh \
#     --pid <worker-pid> \
#     --tail <path-to-tail-file> \
#     --controller-pid <controller-pid> \
#     --agent <agent-label, e.g. codex-bash-tail> \
#     --session-id <slug> \
#     [--ignore-prompt-file <path>] \
#     [--markers <regex>] \
#     [--poll-secs <N>] \
#     [--max-idle-secs <N>]
#
# Defaults:
#   --markers       defaults to goalflight_watch.SHELL_TERMINAL_MARKER_RE
#                   (terminal-marker subset; emphasis-tolerant for grok's **MARKER:**)
#   --poll-secs     15
#   --max-idle-secs 180   (matches protocol idle/no-progress guidance)
#   GOALFLIGHT_WATCH_TOTAL_RUNTIME_SECS defaults to 10x max-idle (direct-call bound)
#   --cpu-epsilon   0.1   (process-group CPU % of one core above this is running_quiet;
#                          measured as a cputime delta, not ps's decaying %cpu average)
#
# Intended to be backgrounded by commands/execute.md (the bash-tail dispatch branch):
#   bash <skill-root>/scripts/watch-dispatch-tail.sh \
#     --pid $WORKER_PID --tail /tmp/codex-<slug>.txt \
#     --controller-pid $$ --agent codex-bash-tail --session-id <slug> \
#     > /tmp/watcher-<slug>.txt 2>&1 &
# Then dispatch a Bash watcher with run_in_background: true that simply
# does `wait $WATCHER_PID` and surfaces the watcher's exit code + tail file
# back through the task-notification.

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Process-group CPU as percent of ONE core, mirrored from
# scripts/goalflight_liveness.py (pgroup_cpu_pct / cpu_pct_from_cputime_delta).
#
# Why not ps %cpu: on Darwin %cpu is a DECAYING AVERAGE, measured up to 4x wrong
# in both directions at transitions (ps 95.4% for a process truly at 25% just
# after it stopped burning; ps 0.7% for one truly at 9.2% doing short bursts).
# Those transitions are exactly when this watcher decides working vs wedged.
# Differencing cumulative cpu-time (`ps -o time=`) across two samples measures
# the rate over the window that actually elapsed.
#
# Warm path: the main poll loop keeps the previous sample between iterations so
# each tick costs one ps sweep (no sleep). Cold path (first call / aged-out
# cache): take a second sample after a short deliberate pause — a rate cannot
# be read from a single cumulative counter.
#
# Sample format: newline-separated "pid cputime_seconds" for pids in the group.
# Cache is process-local to this watcher (one pgid at a time).
_CPU_SAMPLE_WARM_MIN_S=0.2
_CPU_SAMPLE_COLD_WINDOW_S=0.6
_CPU_SAMPLE_MAX_AGE_S=60
_CPU_CACHE_PGID=""
_CPU_CACHE_TS=""
_CPU_CACHE_SAMPLE=""

# Monotonic seconds as a float. date +%s is whole-second and wall-clock (jumps
# on sleep/NTP); python time.monotonic matches the python twin and resolves the
# cold-window sub-second pause. python3 is a hard dep of this script.
_cpu_monotonic_now() {
  python3 -c 'import time; print("%.6f" % time.monotonic())' 2>/dev/null
}

# Parse a ps TIME field -- [[DD-]HH:]MM:SS[.ss] -- into seconds.
# Derivation: ps omits leading zero groups, so the rightmost colon field is
# always seconds, then minutes, then hours (powers of 60 right-to-left). A
# leading "N-" is whole days. Left-to-right accumulation (secs = secs*60 + part)
# is algebraically identical to the right-to-left sum and is what awk does here.
# Unit check: "1:02:03" -> 3723; "0:00.06" -> 0.06; "2-03:04:05" -> 183845.
parse_ps_cputime() {
  # $1 = field; prints seconds on stdout. Empty / unparsable -> exit 1.
  local field="$1"
  [ -n "$field" ] || return 1
  printf '%s\n' "$field" | awk '
    {
      text = $0
      days = 0
      if (index(text, "-") > 0) {
        split(text, dparts, "-")
        days = dparts[1] + 0
        text = dparts[2]
      }
      n = split(text, parts, ":")
      if (n < 1) { exit 1 }
      secs = 0
      for (i = 1; i <= n; i++) {
        secs = secs * 60 + (parts[i] + 0)
      }
      printf "%.6f\n", days * 86400 + secs
      exit 0
    }'
}

# One ps sweep for a process group -> "pid seconds" lines on stdout.
# Returns 1 only when the ps sample itself is unavailable (caller -> "unknown").
# An empty group (no matching pids) is a successful empty sample, not unknown.
pgroup_cputime_snapshot() {
  local pgid="$1"
  local raw
  if ! raw=$(ps -A -o pgid=,pid=,time= 2>/dev/null); then
    return 1
  fi
  printf '%s\n' "$raw" | awk -v target="$pgid" '
    function parse_time(field,    days, n, parts, i, secs, text) {
      text = field
      days = 0
      if (index(text, "-") > 0) {
        split(text, dparts, "-")
        days = dparts[1] + 0
        text = dparts[2]
      }
      n = split(text, parts, ":")
      secs = 0
      for (i = 1; i <= n; i++) {
        secs = secs * 60 + (parts[i] + 0)
      }
      return days * 86400 + secs
    }
    NF >= 3 && ($1 + 0) == (target + 0) {
      printf "%s %.6f\n", $2, parse_time($3)
    }
  '
  return 0
}

# Percent of one core burned by a process group over window_s.
# Paired PER PID rather than summing the group (see python twin docstring):
#   - exited child: drop it (do not go negative)
#   - born child: count its full cpu-time (all of it is in-window)
#   - matched pid: after - before when positive
cpu_pct_from_cputime_delta() {
  local before="$1"
  local after="$2"
  local window="$3"
  # awk gets the two samples via env-style -v; newlines stay intact in gawk/nawk
  # when passed as -v values on modern macOS awk. Fall back through printf pipe
  # if a platform ever strips them: keep the separator form below.
  {
    printf '%s\n' "$before"
    printf '%s\n' "--"
    printf '%s\n' "$after"
  } | awk -v window="$window" '
    BEGIN {
      side = 0
      # Nonpositive window: a rate is undefined. Print once in END (awk still
      # runs END after exit-from-BEGIN, so printing here would double the line).
    }
    $0 == "--" { side = 1; next }
    NF >= 2 {
      pid = $1
      secs = $2 + 0
      if (side == 0) {
        before[pid] = secs
      } else {
        after_pids[pid] = secs
      }
    }
    END {
      if ((window + 0) <= 0) {
        printf "0.0\n"
        exit 0
      }
      busy = 0
      for (pid in after_pids) {
        a = after_pids[pid]
        if (!(pid in before)) {
          busy += a
        } else if (a > before[pid]) {
          busy += a - before[pid]
        }
      }
      if (busy < 0) busy = 0
      printf "%.1f\n", busy / window * 100.0
    }
  '
}

pgroup_cpu_pct() {
  local pgid="$1"
  local now sample window later later_sample pct

  if [ -z "$pgid" ]; then
    echo "unknown"
    return 0
  fi

  now=$(_cpu_monotonic_now) || true
  if [ -z "$now" ]; then
    echo "unknown"
    return 0
  fi

  if ! sample=$(pgroup_cputime_snapshot "$pgid"); then
    echo "unknown"
    return 0
  fi

  # Warm path: difference against the previous loop iteration's sample.
  if [ -n "$_CPU_CACHE_TS" ] \
     && [ "$_CPU_CACHE_PGID" = "$pgid" ]; then
    window=$(awk -v now="$now" -v prev="$_CPU_CACHE_TS" \
      'BEGIN { printf "%.6f", now - prev }')
    if awk -v w="$window" \
           -v mn="$_CPU_SAMPLE_WARM_MIN_S" \
           -v mx="$_CPU_SAMPLE_MAX_AGE_S" \
           'BEGIN { exit !((w + 0) >= (mn + 0) && (w + 0) <= (mx + 0)) }'; then
      pct=$(cpu_pct_from_cputime_delta "$_CPU_CACHE_SAMPLE" "$sample" "$window")
      _CPU_CACHE_PGID="$pgid"
      _CPU_CACHE_TS="$now"
      _CPU_CACHE_SAMPLE="$sample"
      printf '%s\n' "$pct"
      return 0
    fi
  fi

  # Cold path: no usable cached sample. One deliberate short sleep to build a
  # rate — same shape as goalflight_liveness.pgroup_cpu_pct.
  sleep "$_CPU_SAMPLE_COLD_WINDOW_S"
  later=$(_cpu_monotonic_now) || true
  if [ -z "$later" ]; then
    echo "unknown"
    return 0
  fi
  if ! later_sample=$(pgroup_cputime_snapshot "$pgid"); then
    echo "unknown"
    return 0
  fi
  window=$(awk -v now="$later" -v prev="$now" \
    'BEGIN { printf "%.6f", now - prev }')
  pct=$(cpu_pct_from_cputime_delta "$sample" "$later_sample" "$window")
  _CPU_CACHE_PGID="$pgid"
  _CPU_CACHE_TS="$later"
  _CPU_CACHE_SAMPLE="$later_sample"
  printf '%s\n' "$pct"
  return 0
}

# Live descendants of a worker pid, excluding itself. "unknown" when the
# sample is unavailable. A quiet-but-working child (pytest, sleep, a
# compiler) must veto idle-timeout even at 0% CPU.
live_descendant_count() {
  local root="$1"
  if [ -z "$root" ]; then
    echo "unknown"
    return 0
  fi
  ps -axo pid=,ppid= 2>/dev/null | awk -v root="$root" '
    BEGIN { root = root + 0 }
    NF >= 2 {
      c = $1 + 0
      p = $2 + 0
      kids[p] = kids[p] " " c
      parsed++
    }
    END {
      if (parsed + 0 < 1) {
        print "unknown"
        exit 0
      }
      n = 0
      qn = 1
      q[1] = root
      seen[root] = 1
      for (i = 1; i <= qn; i++) {
        nsplit = split(kids[q[i]], arr, " ")
        for (j = 1; j <= nsplit; j++) {
          k = arr[j] + 0
          if (k == 0 || seen[k]) continue
          seen[k] = 1
          n++
          q[++qn] = k
        }
      }
      print n
    }
  '
}
# Pure CPU helpers above are sourceable for unit tests without running the watcher:
#   GOALFLIGHT_WATCH_HELPERS_ONLY=1 source scripts/watch-dispatch-tail.sh
if [ "${GOALFLIGHT_WATCH_HELPERS_ONLY:-}" = "1" ]; then
  return 0 2>/dev/null || exit 0
fi


default_marker_re() {
  PYTHONPATH="$SCRIPT_DIR" python3 - <<'PY'
from goalflight_watch import SHELL_TERMINAL_MARKER_RE
print(SHELL_TERMINAL_MARKER_RE)
PY
}

WORKER_PID=""
TAIL_PATH=""
CONTROLLER_PID=""
AGENT_LABEL=""
SESSION_ID=""
IGNORE_PROMPT_FILE=""
DEFAULT_MARKER_RE="$(default_marker_re)"
MARKER_RE="$DEFAULT_MARKER_RE"
POLL_SECS=15
MAX_IDLE_SECS=180
CPU_EPSILON=0.1
PID_DEAD_MARKER_GRACE_SECS=1
POST_TERMINAL_EXIT_GRACE_SECS=6
# CPU-sampling-failure grace (codex 2026-05-20 P2): require this many consecutive
# wedged polls before exiting idle-timeout, so transient `ps` low/zero samples
# can't false-positive a healthy CPU-busy worker. Full-suite load on macOS can
# produce several low process-group samples even while the worker is active.
# Not a flag — this is the watcher mirror of the runner's intra-decision
# re-sample grace (goalflight_liveness.cpu_liveness_keep_waiting).
WEDGE_CONFIRM_SAMPLES=5
# Pidfile dir. Honors $GOAL_FLIGHT_PIDFILE_DIR so tests can redirect registration
# into an isolated temp dir. Default is unchanged, so in production the watcher and
# scripts/acp_client.py still share /tmp/goal-flight-acp-pids.d and cleanup_ghosts
# reaps uniformly across both dispatch paths.
PIDFILE_DIR="${GOAL_FLIGHT_PIDFILE_DIR:-/tmp/goal-flight-acp-pids.d}"

usage() {
  sed -n '1,/^$/p' "$0" >&2
  exit 64
}

while [ $# -gt 0 ]; do
  case "$1" in
    --pid)            WORKER_PID="$2"; shift 2 ;;
    --tail)           TAIL_PATH="$2"; shift 2 ;;
    --controller-pid) CONTROLLER_PID="$2"; shift 2 ;;
    --agent)          AGENT_LABEL="$2"; shift 2 ;;
    --session-id)     SESSION_ID="$2"; shift 2 ;;
    --ignore-prompt-file) IGNORE_PROMPT_FILE="$2"; shift 2 ;;
    --markers)        MARKER_RE="$2"; shift 2 ;;
    --poll-secs)      POLL_SECS="$2"; shift 2 ;;
    --max-idle-secs)  MAX_IDLE_SECS="$2"; shift 2 ;;
    --cpu-epsilon)    CPU_EPSILON="$2"; shift 2 ;;
    -h|--help)        usage ;;
    *) echo "unknown arg: $1" >&2; usage ;;
  esac
done

# Direct invocations need a hard lifetime even when a forever-chatty worker
# continuously resets the idle clock. Managed callers already impose their own
# process timeout (scripts/hosts/controller/common.py:132), so this generous
# default is primarily a standalone-watcher backstop. The environment override
# keeps short hermetic tests and unusual direct callers configurable without
# expanding the public CLI surface.
TOTAL_RUNTIME_SECS="${GOALFLIGHT_WATCH_TOTAL_RUNTIME_SECS:-$(( MAX_IDLE_SECS * 10 ))}"
case "$TOTAL_RUNTIME_SECS" in
  ''|*[!0-9]*)
    echo "invalid GOALFLIGHT_WATCH_TOTAL_RUNTIME_SECS '$TOTAL_RUNTIME_SECS' (must be a positive integer)" >&2
    exit 64
    ;;
esac
if [ "$TOTAL_RUNTIME_SECS" -le 0 ]; then
  echo "invalid GOALFLIGHT_WATCH_TOTAL_RUNTIME_SECS '$TOTAL_RUNTIME_SECS' (must be a positive integer)" >&2
  exit 64
fi

# Map of REQUIRED_VAR → --flag-name for missing-arg diagnostics. Spelled
# out long-form (rather than computed via `${var,,}`) because that bash 4+
# lowercase substitution fails on macOS default bash 3.2 with "bad
# substitution" and `tr` would be portable but uglier than this static map.
for required in WORKER_PID TAIL_PATH CONTROLLER_PID AGENT_LABEL SESSION_ID; do
  if [ -z "${!required}" ]; then
    case "$required" in
      WORKER_PID)     flag='--pid' ;;
      TAIL_PATH)      flag='--tail' ;;
      CONTROLLER_PID) flag='--controller-pid' ;;
      AGENT_LABEL)    flag='--agent' ;;
      SESSION_ID)     flag='--session-id' ;;
    esac
    echo "missing required arg: $flag" >&2
    usage
  fi
done

# Validate PID args are integers — without this, a non-integer WORKER_PID
# produces invalid JSON in the pidfile body ({"pid": abc, ...}), which
# cleanup_ghosts json.JSONDecodeError-skips but leaks the file.
case "$WORKER_PID" in
  ''|*[!0-9]*) echo "invalid --pid '$WORKER_PID' (must be integer)" >&2; usage ;;
esac
case "$CONTROLLER_PID" in
  ''|*[!0-9]*) echo "invalid --controller-pid '$CONTROLLER_PID' (must be integer)" >&2; usage ;;
esac

# Hard dep: python3 for json_escape (could fall back to pure-bash escape
# but the inputs include agent labels and slugs that may contain shell
# metacharacters; python3's json.dumps is the safe path). Fail fast if
# missing rather than producing a malformed pidfile body later.
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 required on PATH for pidfile JSON encoding; install or skip --agent labels with special characters" >&2
  exit 70  # EX_SOFTWARE
fi

# Pidfile registration. Schema mirrors scripts/acp_client.py _save_pids():
#   pid, pgid, started_at (ps lstart), cmd (ps comm), agent, session_id
# Filename: <controller-pid>.bashtail.<worker-pid>.jsonl  — single-entry file
# per watcher. cleanup_ghosts() in acp_client.py extracts controller-pid from
# the leading int prefix (the dotted-suffix pattern is preserved through the
# stem-split done there).
PIDFILE="$PIDFILE_DIR/${CONTROLLER_PID}.bashtail.${WORKER_PID}.jsonl"

# Capture identity for the cleanup_ghosts identity check.
# ps -o lstart=,comm= -p <pid> is POSIX-portable across Mac and Linux.
ps_meta() {
  local pid="$1"
  ps -o lstart=,comm= -p "$pid" 2>/dev/null | head -1
}

worker_pgid_current() {
  local pid="$1"
  ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' '
}

cpu_gt_epsilon() {
  local cpu="$1"
  case "$cpu" in
    ""|unknown) return 2 ;;
  esac
  awk -v cpu="$cpu" -v eps="$CPU_EPSILON" 'BEGIN { exit ! ((cpu + 0) > (eps + 0)) }'
}

worker_lstart_comm=""
PS_AVAILABLE=1
if ! ps -o pid= -p "$$" >/dev/null 2>&1; then
  PS_AVAILABLE=0
fi
if [ "$PS_AVAILABLE" -eq 0 ]; then
  if kill -0 "$WORKER_PID" 2>/dev/null; then
    worker_lstart="unknown"
    worker_comm="unknown"
  else
    echo "watcher: worker PID $WORKER_PID not alive at startup; exiting 1" >&2
    exit 1
  fi
else
  for _attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    worker_lstart_comm=$(ps_meta "$WORKER_PID")
    [ -n "$worker_lstart_comm" ] && break
    kill -0 "$WORKER_PID" 2>/dev/null || break
    sleep 0.1
  done
  if [ -z "$worker_lstart_comm" ]; then
    echo "watcher: worker PID $WORKER_PID not alive at startup; exiting 1" >&2
    exit 1
  fi
  # Split: lstart is the first 5 whitespace tokens, comm is the rest.
  worker_lstart=$(echo "$worker_lstart_comm" | awk '{print $1, $2, $3, $4, $5}')
  worker_comm=$(echo "$worker_lstart_comm" | awk '{for (i=6; i<=NF; i++) printf "%s%s", $i, (i<NF ? " " : "")}')
fi

worker_pgid=""
if [ "$PS_AVAILABLE" -ne 0 ]; then
  for _attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    worker_pgid=$(ps -o pgid= -p "$WORKER_PID" 2>/dev/null | tr -d ' ')
    [ -n "$worker_pgid" ] && break
    kill -0 "$WORKER_PID" 2>/dev/null || break
    sleep 0.1
  done
fi
[ -z "$worker_pgid" ] && worker_pgid="$WORKER_PID"

mkdir -p "$PIDFILE_DIR"

# JSON-encode strings safely. printf %s + python is more reliable than shell escaping.
json_escape() { python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$1"; }

cat > "$PIDFILE" <<EOF
{"pid": $WORKER_PID, "pgid": $worker_pgid, "started_at": $(json_escape "$worker_lstart"), "cmd": $(json_escape "$worker_comm"), "agent": $(json_escape "$AGENT_LABEL"), "session_id": $(json_escape "$SESSION_ID")}
EOF

# Pidfile cleanup: preserve the entry when the WORKER is still alive, remove
# only when the worker is definitely gone. Rationale: exit paths where the
# worker may still be alive include:
#   - exit 2 (idle-timeout): worker is wedged but the process is still running;
#                            cleanup_ghosts() should reap it on the next
#                            orchestrator startup
#   - exit 3 (controller-dead): worker may keep running with no supervisor;
#                              cleanup_ghosts() needs the pidfile to reap it
#   - SIGTERM of watcher itself: worker survives; need cleanup_ghosts coverage
# Removing the pidfile in those cases ORPHANS the worker beyond cleanup_ghosts'
# reach. The check is `kill -0 $WORKER_PID` which is cheap and atomic enough.
cleanup_pidfile_on_exit() {
  if [ -n "$WORKER_PID" ] && kill -0 "$WORKER_PID" 2>/dev/null; then
    : # worker still alive — leave pidfile for cleanup_ghosts() on next controller start
  else
    rm -f "$PIDFILE"
  fi
}
trap cleanup_pidfile_on_exit EXIT INT TERM

# Track tail file size for idle detection. Re-stat at each poll.
last_size=0
last_size_change_ts=$(date +%s)
runtime_started_ts=$last_size_change_ts
wedge_streak=0
if [ -f "$TAIL_PATH" ]; then
  last_size=$(wc -c < "$TAIL_PATH" 2>/dev/null | tr -d ' ')
  last_size=${last_size:-0}
fi

# Suspend/sleep grace: if the gap between consecutive watcher polls vastly exceeds
# the poll cadence, the WATCHER itself was suspended (laptop sleep) — wall-clock
# idle accounting across that gap is invalid (the worker was suspended too, not
# idle). Observed 2026-06-09: lid-close sleep produced phantom idle_for >> max-idle
# on wake and killed two healthy mid-verify codex workers (macOS ps %cpu also reads
# ~0 right after wake, defeating the CPU/wedge grace).
SLEEP_GAP_GRACE_SECS=$(( POLL_SECS * 5 + 120 ))
prev_loop_ts=$(date +%s)

echo "[watcher start $(date '+%H:%M:%S')] worker_pid=$WORKER_PID controller_pid=$CONTROLLER_PID tail=$TAIL_PATH markers='$MARKER_RE' poll=${POLL_SECS}s max_idle=${MAX_IDLE_SECS}s total_runtime=${TOTAL_RUNTIME_SECS}s"

terminal_marker_seen() {
  [ -f "$TAIL_PATH" ] || return 1
  if [ "$MARKER_RE" != "$DEFAULT_MARKER_RE" ]; then
    # A custom regex is an additional filter, never an alternate identity
    # grammar. The shared parser must first bind the terminal payload to this
    # dispatch, so a foreign marker cannot ride through the legacy override.
    PYTHONPATH="$SCRIPT_DIR" python3 - "$TAIL_PATH" "${IGNORE_PROMPT_FILE:-}" "$MARKER_RE" "$AGENT_LABEL" "$SESSION_ID" <<'PY'
import pathlib
import re
import sys

tail = pathlib.Path(sys.argv[1])
prompt_arg = sys.argv[2]
marker_re = re.compile(sys.argv[3])
agent = sys.argv[4]
dispatch_id = sys.argv[5]
prompt_lines = []
if prompt_arg:
    prompt = pathlib.Path(prompt_arg)
    if prompt.exists():
        prompt_lines = [line.strip() for line in prompt.read_text(encoding="utf-8", errors="replace").splitlines()]

from goalflight_agent_limits import moonshot_family
from goalflight_watch import _last_line_is_terminal_marker

marker = _last_line_is_terminal_marker(
    tail,
    ignore_prefix_lines=prompt_lines,
    kimi_output=moonshot_family(agent),
    expected_dispatch_id=dispatch_id,
)
lines = tail.read_text(encoding="utf-8", errors="replace").splitlines()
raw_line = lines[int(marker["line"]) - 1] if marker else ""
if marker and marker_re.search(raw_line.strip()):
    print(f"{marker['line']}:{marker['kind']}:{marker['text']}")
    raise SystemExit(0)
raise SystemExit(1)
PY
    return $?
  fi
  PYTHONPATH="$SCRIPT_DIR" python3 - "$TAIL_PATH" "${IGNORE_PROMPT_FILE:-}" "$AGENT_LABEL" "$SESSION_ID" <<'PY'
import pathlib
import sys

from goalflight_agent_limits import moonshot_family
from goalflight_watch import _last_line_is_terminal_marker

tail = pathlib.Path(sys.argv[1])
prompt_arg = sys.argv[2]
agent = sys.argv[3]
dispatch_id = sys.argv[4]
prompt_lines = []
if prompt_arg:
    prompt = pathlib.Path(prompt_arg)
    if prompt.exists():
        prompt_lines = [line.strip() for line in prompt.read_text(encoding="utf-8", errors="replace").splitlines()]
marker = _last_line_is_terminal_marker(
    tail,
    ignore_prefix_lines=prompt_lines,
    kimi_output=moonshot_family(agent),
    expected_dispatch_id=dispatch_id,
)
if marker:
    print(f"{marker['line']}:{marker['kind']}:{marker['text']}")
    raise SystemExit(0)
raise SystemExit(1)
PY
}

final_terminal_marker() {
  [ -f "$TAIL_PATH" ] || return 1
  PYTHONPATH="$SCRIPT_DIR" python3 - "$TAIL_PATH" "${IGNORE_PROMPT_FILE:-}" "$AGENT_LABEL" "$SESSION_ID" <<'PY'
import pathlib
import sys

from goalflight_agent_limits import moonshot_family
from goalflight_watch import _final_terminal_marker

tail = pathlib.Path(sys.argv[1])
prompt_arg = sys.argv[2]
agent = sys.argv[3]
dispatch_id = sys.argv[4]
prompt_lines = []
if prompt_arg:
    prompt = pathlib.Path(prompt_arg)
    if prompt.exists():
        prompt_lines = [line.strip() for line in prompt.read_text(encoding="utf-8", errors="replace").splitlines()]
marker = _final_terminal_marker(
    tail,
    ignore_prefix_lines=prompt_lines,
    kimi_output=moonshot_family(agent),
    expected_dispatch_id=dispatch_id,
)
if marker:
    print(f"{marker['line']}:{marker['kind']}:{marker['text']}")
    raise SystemExit(0)
raise SystemExit(1)
PY
}

emit_marker_exit() {
  local detail="${1:-terminal marker matched in tail}"
  local exit_code="${2:-0}"
  echo "[$(date '+%H:%M:%S')] $detail"
  echo "=== tail last 30 lines ==="
  tail -30 "$TAIL_PATH"
  echo "WATCHER-EXIT: marker exit_code=$exit_code"
  exit "$exit_code"
}

emit_runtime_timeout() {
  local elapsed="$1"
  echo "[$(date '+%H:%M:%S')] watcher total runtime ${elapsed}s reached ${TOTAL_RUNTIME_SECS}s bound"
  if [ -f "$TAIL_PATH" ]; then
    echo "=== tail last 30 lines ==="
    tail -30 "$TAIL_PATH"
  fi
  echo "WATCHER-EXIT: runtime-timeout exit_code=2"
  exit 2
}

exit_code_for_reconciled_marker_kind() {
  case "$1" in
    COMPLETE|RESULT|READY) echo 0 ;;
    BLOCKED|FAILED) echo 4 ;;
    USER-NEED|USER-CONFIRM) echo 0 ;;
    *) echo 0 ;;
  esac
}

while true; do
  # 0. Suspend/sleep detection — reset idle clock across suspend gaps.
  now_loop_ts=$(date +%s)
  loop_gap=$(( now_loop_ts - prev_loop_ts ))
  if [ "$loop_gap" -gt "$SLEEP_GAP_GRACE_SECS" ]; then
    echo "[$(date '+%H:%M:%S')] WATCHER-STATE: suspend-gap detected (${loop_gap}s between polls > ${SLEEP_GAP_GRACE_SECS}s grace) — resetting idle clock (system slept; worker not idle)"
    last_size_change_ts=$now_loop_ts
    runtime_started_ts=$(( runtime_started_ts + loop_gap ))
    wedge_streak=0
  fi
  prev_loop_ts=$now_loop_ts

  # 1. Orchestrator alive? (orphan watcher self-detection)
  if ! kill -0 "$CONTROLLER_PID" 2>/dev/null; then
    echo "[$(date '+%H:%M:%S')] controller PID $CONTROLLER_PID is gone"
    if [ -f "$TAIL_PATH" ]; then
      echo "=== tail last 30 lines ==="
      tail -30 "$TAIL_PATH"
    fi
    echo "WATCHER-EXIT: controller-dead exit_code=3"
    exit 3
  fi

  runtime_elapsed=$(( now_loop_ts - runtime_started_ts ))
  if [ "$runtime_elapsed" -ge "$TOTAL_RUNTIME_SECS" ]; then
    emit_runtime_timeout "$runtime_elapsed"
  fi

  # 2. Terminal marker in tail?
  # Hardening (C-P1/D-P1 marker injection): only the LAST non-empty line counts as
  # a terminal. A worker that prints/cats/logs a marker token mid-output must not
  # false-complete the watcher. (Python watcher + acp_runner also fence-skip; this
  # legacy bash path checks last-non-empty-line.)
  seen_marker=$(terminal_marker_seen 2>/dev/null || true)
  if [ -n "$seen_marker" ]; then
    seen_kind=${seen_marker#*:}
    seen_kind=${seen_kind%%:*}
    seen_exit_code=$(exit_code_for_reconciled_marker_kind "$seen_kind")
    case "$seen_kind" in
      COMPLETE|RESULT|READY)
        if kill -0 "$WORKER_PID" 2>/dev/null; then
          candidate_size=$(wc -c < "$TAIL_PATH" 2>/dev/null | tr -d ' ')
          candidate_size=${candidate_size:-0}
          candidate_started=$(date +%s)
          candidate_discarded=0
          while kill -0 "$WORKER_PID" 2>/dev/null; do
            candidate_now=$(date +%s)
            candidate_idle_for=$(( candidate_now - candidate_started ))
            candidate_runtime_elapsed=$(( candidate_now - runtime_started_ts ))
            if [ "$candidate_runtime_elapsed" -ge "$TOTAL_RUNTIME_SECS" ]; then
              emit_runtime_timeout "$candidate_runtime_elapsed"
            fi
            if [ "$candidate_idle_for" -ge "$POST_TERMINAL_EXIT_GRACE_SECS" ]; then
              echo "[$(date '+%H:%M:%S')] WATCHER-STATE: terminal candidate still pending while worker is alive (${candidate_idle_for}s no growth; $seen_marker)"
              break
            fi
            sleep "$POLL_SECS"
            candidate_current_size=$(wc -c < "$TAIL_PATH" 2>/dev/null | tr -d ' ')
            candidate_current_size=${candidate_current_size:-0}
            if [ "$candidate_current_size" -gt "$candidate_size" ]; then
              echo "[$(date '+%H:%M:%S')] WATCHER-DISCARD: terminal candidate disproved by live tail growth (${candidate_size}->${candidate_current_size}; $seen_marker)"
              candidate_discarded=1
              break
            fi
          done
          if [ "$candidate_discarded" -eq 1 ]; then
            continue
          fi
          # The worker exited during the grace. Fall through to the pid-dead
          # reconciliation below so historical post-marker output is included.
        else
          emit_marker_exit "terminal marker matched after worker exit ($seen_marker)" "$seen_exit_code"
        fi
        ;;
      *)
        # Blocking/user-decision terminals must wake the controller even while
        # the worker waits for a response; the live-growth veto is success-only.
        emit_marker_exit "terminal marker matched in tail ($seen_marker)" "$seen_exit_code"
        ;;
    esac
  fi

  # 3. Worker PID still alive?
  if ! kill -0 "$WORKER_PID" 2>/dev/null; then
    # The tail writer can flush a terminal marker just after the worker exits
    # and just after the loop's first marker check. Give that marker precedence.
    sleep "$PID_DEAD_MARKER_GRACE_SECS"
    seen_marker=$(terminal_marker_seen 2>/dev/null || true)
    if [ -n "$seen_marker" ]; then
      seen_kind=${seen_marker#*:}
      seen_kind=${seen_kind%%:*}
      seen_exit_code=$(exit_code_for_reconciled_marker_kind "$seen_kind")
      emit_marker_exit "terminal marker matched after pid-dead grace ($seen_marker)" "$seen_exit_code"
    fi
    reconciled_marker=$(final_terminal_marker 2>/dev/null || true)
    if [ -n "$reconciled_marker" ]; then
      reconciled_kind=${reconciled_marker#*:}
      reconciled_kind=${reconciled_kind%%:*}
      reconciled_exit_code=$(exit_code_for_reconciled_marker_kind "$reconciled_kind")
      emit_marker_exit "terminal marker reconciled after worker exit ($reconciled_marker)" "$reconciled_exit_code"
    fi
    echo "[$(date '+%H:%M:%S')] worker PID $WORKER_PID is gone (no terminal marker seen after pid-dead grace)"
    if [ -f "$TAIL_PATH" ]; then
      echo "=== tail last 30 lines ==="
      tail -30 "$TAIL_PATH"
    fi
    echo "WATCHER-EXIT: pid-dead exit_code=1"
    exit 1
  fi

  # 4. Idle timeout? Tail-size silence is not enough: a worker with live
  #    children (or process-group CPU) is still working.
  if [ -f "$TAIL_PATH" ]; then
    cur_size=$(wc -c < "$TAIL_PATH" 2>/dev/null | tr -d ' ')
    cur_size=${cur_size:-0}
    if [ "$cur_size" -ne "$last_size" ]; then
      last_size="$cur_size"
      last_size_change_ts=$(date +%s)
      wedge_streak=0   # worker made progress — reset the wedge confirm streak
    else
      now_ts=$(date +%s)
      idle_for=$(( now_ts - last_size_change_ts ))
      if [ "$idle_for" -ge "$MAX_IDLE_SECS" ]; then
        current_worker_pgid=$(worker_pgid_current "$WORKER_PID")
        [ -n "$current_worker_pgid" ] && worker_pgid="$current_worker_pgid"
        cpu_pct=$(pgroup_cpu_pct "$worker_pgid")
        cpu_gt_epsilon "$cpu_pct"
        cpu_check_rc=$?
        if [ "$cpu_check_rc" -eq 0 ]; then
          wedge_streak=0
          echo "[$(date '+%H:%M:%S')] WATCHER-STATE: running_quiet worker_pid=$WORKER_PID pgid=$worker_pgid pgroup_cpu_pct=$cpu_pct idle_for=${idle_for}s (worker-or-child CPU active)"
          sleep "$POLL_SECS"
          continue
        fi
        if [ "$cpu_check_rc" -eq 2 ]; then
          echo "[$(date '+%H:%M:%S')] WATCHER-STATE: running_quiet worker_pid=$WORKER_PID pgid=$worker_pgid pgroup_cpu_pct=unknown idle_for=${idle_for}s (cpu unavailable; unknown is not idle)"
          sleep "$POLL_SECS"
          continue
        fi
        desc_count=$(live_descendant_count "$WORKER_PID")
        if [ "$desc_count" = "unknown" ]; then
          echo "[$(date '+%H:%M:%S')] WATCHER-STATE: running_quiet worker_pid=$WORKER_PID pgid=$worker_pgid pgroup_cpu_pct=$cpu_pct live_descendants=unknown idle_for=${idle_for}s (descendants unavailable; unknown is not idle)"
          sleep "$POLL_SECS"
          continue
        fi
        if [ "$desc_count" -gt 0 ]; then
          wedge_streak=0
          echo "[$(date '+%H:%M:%S')] WATCHER-STATE: running_quiet worker_pid=$WORKER_PID pgid=$worker_pgid pgroup_cpu_pct=$cpu_pct live_descendants=$desc_count idle_for=${idle_for}s (live child; tail-quiet is not idle)"
          sleep "$POLL_SECS"
          continue
        fi
        # CPU at/below epsilon: looks wedged. Require consecutive confirmations
        # so a single transient `ps` failure (cpu→0.0 for one poll) can't
        # false-positive a healthy worker into idle-timeout (codex P2 grace).
        if [ "$cpu_check_rc" -ne 2 ]; then
          wedge_streak=$(( wedge_streak + 1 ))
        fi
        if [ "$wedge_streak" -lt "$WEDGE_CONFIRM_SAMPLES" ]; then
          echo "[$(date '+%H:%M:%S')] WATCHER-STATE: wedge-unconfirmed ($wedge_streak/$WEDGE_CONFIRM_SAMPLES) worker_pid=$WORKER_PID pgid=$worker_pgid pgroup_cpu_pct=$cpu_pct idle_for=${idle_for}s — re-checking"
          sleep "$POLL_SECS"
          continue
        fi
        echo "[$(date '+%H:%M:%S')] tail file idle for ${idle_for}s (>= ${MAX_IDLE_SECS}s threshold) — worker likely wedged"
        echo "=== tail last 30 lines ==="
        tail -30 "$TAIL_PATH"
        echo "WATCHER-EXIT: idle-timeout exit_code=2"
        exit 2
      fi
    fi
  fi

  sleep "$POLL_SECS"
done
