#!/usr/bin/env bash
# watch-dispatch-tail.sh — content-aware completion watcher for [bash-tail] dispatches.
#
# Watches a worker's tail file for any TERMINAL marker (COMPLETE / BLOCKED /
# USER-NEED / USER-CONFIRM, with optional markdown emphasis tolerance for grok).
# Exits when:
#   - terminal marker observed in tail            → exit 0  ("WATCHER-EXIT: marker")
#   - worker PID dies without terminal marker     → exit 1  ("WATCHER-EXIT: pid-dead")
#   - no tail update for --max-idle-secs seconds  → exit 2  ("WATCHER-EXIT: idle-timeout")
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
#     [--markers <regex>] \
#     [--poll-secs <N>] \
#     [--max-idle-secs <N>]
#
# Defaults:
#   --markers       '^\**(COMPLETE|BLOCKED|USER-NEED|USER-CONFIRM):\**'
#                   (terminal-marker subset; emphasis-tolerant for grok's **MARKER:**)
#   --poll-secs     15
#   --max-idle-secs 180   (matches protocol idle/no-progress guidance)
#   --cpu-epsilon   0.1   (process-group %CPU above this is running_quiet)
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

WORKER_PID=""
TAIL_PATH=""
CONTROLLER_PID=""
AGENT_LABEL=""
SESSION_ID=""
MARKER_RE='^\**(COMPLETE|BLOCKED|USER-NEED|USER-CONFIRM|READY):\**'
POLL_SECS=15
MAX_IDLE_SECS=180
CPU_EPSILON=0.1
PID_DEAD_MARKER_GRACE_SECS=1
# CPU-sampling-failure grace (codex 2026-05-20 P2): require this many consecutive
# wedged polls before exiting idle-timeout, so one transient `ps` failure can't
# false-positive a healthy worker. Not a flag — mirrors goalflight_watch.py. This
# is the watcher mirror of the runner's intra-decision re-sample grace
# (goalflight_liveness.cpu_liveness_keep_waiting) — same goal, keep them aligned.
WEDGE_CONFIRM_SAMPLES=2
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
    --markers)        MARKER_RE="$2"; shift 2 ;;
    --poll-secs)      POLL_SECS="$2"; shift 2 ;;
    --max-idle-secs)  MAX_IDLE_SECS="$2"; shift 2 ;;
    --cpu-epsilon)    CPU_EPSILON="$2"; shift 2 ;;
    -h|--help)        usage ;;
    *) echo "unknown arg: $1" >&2; usage ;;
  esac
done

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

pgroup_cpu_pct() {
  local pgid="$1"
  ps -A -o pgid=,%cpu= 2>/dev/null | awk -v target="$pgid" '
    BEGIN { sum = 0; found = 0 }
    $1 == target { sum += $2 + 0; found = 1 }
    END {
      if (found) {
        printf "%.1f\n", sum
      } else {
        printf "0.0\n"
      }
    }'
}

worker_pgid_current() {
  local pid="$1"
  ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' '
}

cpu_gt_epsilon() {
  local cpu="$1"
  awk -v cpu="$cpu" -v eps="$CPU_EPSILON" 'BEGIN { exit ! ((cpu + 0) > (eps + 0)) }'
}

worker_lstart_comm=""
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

worker_pgid=""
for _attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  worker_pgid=$(ps -o pgid= -p "$WORKER_PID" 2>/dev/null | tr -d ' ')
  [ -n "$worker_pgid" ] && break
  kill -0 "$WORKER_PID" 2>/dev/null || break
  sleep 0.1
done
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
wedge_streak=0
if [ -f "$TAIL_PATH" ]; then
  last_size=$(wc -c < "$TAIL_PATH" 2>/dev/null | tr -d ' ')
  last_size=${last_size:-0}
fi

echo "[watcher start $(date '+%H:%M:%S')] worker_pid=$WORKER_PID controller_pid=$CONTROLLER_PID tail=$TAIL_PATH markers='$MARKER_RE' poll=${POLL_SECS}s max_idle=${MAX_IDLE_SECS}s"

terminal_marker_seen() {
  [ -f "$TAIL_PATH" ] || return 1
  grep -vE '^[[:space:]]*$' "$TAIL_PATH" 2>/dev/null | tail -1 | grep -qE "$MARKER_RE" 2>/dev/null
}

emit_marker_exit() {
  echo "[$(date '+%H:%M:%S')] terminal marker matched in tail"
  echo "=== tail last 30 lines ==="
  tail -30 "$TAIL_PATH"
  echo "WATCHER-EXIT: marker exit_code=0"
  exit 0
}

while true; do
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

  # 2. Terminal marker in tail?
  # Hardening (C-P1/D-P1 marker injection): only the LAST non-empty line counts as
  # a terminal. A worker that prints/cats/logs a marker token mid-output must not
  # false-complete the watcher. (Python watcher + acp_runner also fence-skip; this
  # legacy bash path checks last-non-empty-line.)
  if terminal_marker_seen; then
    emit_marker_exit
  fi

  # 3. Worker PID still alive?
  if ! kill -0 "$WORKER_PID" 2>/dev/null; then
    # The tail writer can flush a terminal marker just after the worker exits
    # and just after the loop's first marker check. Give that marker precedence.
    sleep "$PID_DEAD_MARKER_GRACE_SECS"
    if terminal_marker_seen; then
      emit_marker_exit
    fi
    echo "[$(date '+%H:%M:%S')] worker PID $WORKER_PID is gone (no terminal marker seen after pid-dead grace)"
    if [ -f "$TAIL_PATH" ]; then
      echo "=== tail last 30 lines ==="
      tail -30 "$TAIL_PATH"
    fi
    echo "WATCHER-EXIT: pid-dead exit_code=1"
    exit 1
  fi

  # 4. Idle timeout? (no tail-size change for max-idle-secs)
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
        if cpu_gt_epsilon "$cpu_pct"; then
          wedge_streak=0
          echo "[$(date '+%H:%M:%S')] WATCHER-STATE: running_quiet worker_pid=$WORKER_PID pgid=$worker_pgid pgroup_cpu_pct=$cpu_pct idle_for=${idle_for}s (worker-or-child CPU active)"
          sleep "$POLL_SECS"
          continue
        fi
        # CPU at/below epsilon: looks wedged. Require consecutive confirmations
        # so a single transient `ps` failure (cpu→0.0 for one poll) can't
        # false-positive a healthy worker into idle-timeout (codex P2 grace).
        wedge_streak=$(( wedge_streak + 1 ))
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
