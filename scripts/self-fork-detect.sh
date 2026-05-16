#!/usr/bin/env bash
# self-fork-detect.sh — write a fork contract before /fork; detect identity after.
#
# Why this exists
# ---------------
# Claude Code's `/fork` (or CLI `--fork-session`) creates a new session that
# inherits the parent's conversation history but gets a new session ID. The
# `CLAUDE_CODE_SESSION_ID` env var inside the fork reflects the NEW ID. This
# lets a session distinguish "I'm the original controller" from "I'm a fork
# branched off to execute a delegated task" by comparing the current env var
# to a marker the controller wrote before forking.
#
# Empirical findings (May 2026, codex CLI 2.1.x):
#   - /fork creates new session ID (env var changes; JSONL is top-level sibling).
#   - Agent-tool subagents INHERIT the parent's CLAUDE_CODE_SESSION_ID
#     (their JSONL is at <proj>/<parent-sid>/subagents/agent-<hash>.jsonl).
#   - This means env-var alone disambiguates fork-vs-original but NOT
#     subagent-vs-original. Subagents shouldn't normally be reading the
#     fork contract (it's a fork-specific affordance), but if they do, the
#     `detect` mode checks for recent activity under `subagents/` and
#     reports SUBAGENT instead of falsely reporting ORIGINAL.
#
# Usage
# -----
#   self-fork-detect.sh write '<task description>' [<contract-path>]
#       Writes the contract file (default: docs-private/.fork-contract.json).
#       Captures current CLAUDE_CODE_SESSION_ID + the task the fork should
#       execute. Run this BEFORE typing /fork.
#
#   self-fork-detect.sh detect [<contract-path>]
#       Reads the contract and prints one of:
#         ORIGINAL    — env matches controller_session_id; I'm the controller.
#         FORK        — env differs; I'm a fork. The task line is also printed.
#         SUBAGENT    — env matches BUT my JSONL is in subagents/. Don't act
#                       on the contract; you're not the agent it was written for.
#         NO_CONTRACT — no contract file; nothing to compare.
#
#   self-fork-detect.sh find-fork [<contract-path>]
#       List top-level JSONLs that didn't exist when `write` was called
#       and aren't the controller's own session. These are the fork
#       candidates the controller can monitor. Prints one path per line
#       (most-recently-modified first). Empty output = no forks detected
#       yet (either user hasn't /fork-ed, or fork is too new to have its
#       JSONL flushed — wait 1-2s and retry).
#
#   self-fork-detect.sh monitor <fork-jsonl> [--poll <sec>] [--idle-stop <sec>]
#       Poll the fork's JSONL for new events. Prints the latest assistant
#       text + tool-call summary as they appear. Exits when:
#         - The JSONL's mtime hasn't changed for --idle-stop seconds
#           (default 120; fork is done or stuck), OR
#         - A line containing "FORK-COMPLETE" is observed (fork signalled
#           completion per the contract).
#       --poll defaults to 5 seconds.
#
#   self-fork-detect.sh clear [<contract-path>]
#       Remove the contract. Run after the fork's work is committed and the
#       contract no longer represents an active delegation.

set -eu

ACTION="${1:-detect}"

case "$ACTION" in
  write)
    TASK="${2:-(no task specified)}"
    CONTRACT="${3:-docs-private/.fork-contract.json}"
    SID="${CLAUDE_CODE_SESSION_ID:-UNSET}"
    if [ "$SID" = "UNSET" ]; then
      echo "ERROR: CLAUDE_CODE_SESSION_ID env var not set." >&2
      echo "  Are you running inside Claude Code? (script needs the env var to capture controller identity.)" >&2
      exit 1
    fi
    mkdir -p "$(dirname "$CONTRACT")"
    python3 - "$CONTRACT" "$TASK" "$SID" <<'PY'
import json, os, sys, datetime, glob
contract_path, task, sid = sys.argv[1], sys.argv[2], sys.argv[3]

# Snapshot existing top-level JSONLs so `find-fork` can diff later.
projects_root = os.path.expanduser("~/.claude/projects")
jsonls_snapshot = []
if os.path.isdir(projects_root):
    for root, dirs, files in os.walk(projects_root):
        # Skip subagents/ subdirectories (those are subagent transcripts, not fork candidates).
        if "subagents" in root.split(os.sep):
            continue
        for f in files:
            if f.endswith(".jsonl"):
                jsonls_snapshot.append(os.path.join(root, f))

contract = {
  "controller_session_id": sid,
  "marker_written_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
  "jsonl_snapshot": sorted(jsonls_snapshot),
  "fork_contract": {
    "task": task,
    "completion_signal": "git commit + RESUME-NOTES rev bump with FORK-COMPLETE marker",
    "abort_signal": "write docs-private/FORK-BLOCKED-<id>.md and stop without committing",
  },
  "_about": "Written by scripts/self-fork-detect.sh before /fork. After /fork, the new session checks identity via `detect`. Controller can use `find-fork` to locate the new session's JSONL (diff against jsonl_snapshot) and `monitor` to watch it.",
}
with open(contract_path, "w") as f:
  json.dump(contract, f, indent=2)
print(f"wrote {contract_path}")
print(f"  controller_session_id: {contract['controller_session_id']}")
print(f"  task: {contract['fork_contract']['task']}")
print(f"  jsonl_snapshot: {len(jsonls_snapshot)} files captured")
PY
    ;;

  find-fork)
    CONTRACT="${2:-docs-private/.fork-contract.json}"
    if [ ! -f "$CONTRACT" ]; then
      echo "ERROR: no contract at $CONTRACT — run 'write' first." >&2
      exit 1
    fi
    SID="${CLAUDE_CODE_SESSION_ID:-UNSET}"
    python3 - "$CONTRACT" "$SID" <<'PY'
import json, os, sys, glob
contract_path, sid = sys.argv[1], sys.argv[2]
contract = json.load(open(contract_path))
snapshot = set(contract.get("jsonl_snapshot", []))
controller_sid = contract["controller_session_id"]

projects_root = os.path.expanduser("~/.claude/projects")
candidates = []
if os.path.isdir(projects_root):
    for root, dirs, files in os.walk(projects_root):
        if "subagents" in root.split(os.sep):
            continue
        for f in files:
            if not f.endswith(".jsonl"):
                continue
            full = os.path.join(root, f)
            if full in snapshot:
                continue  # existed at write time, not a fork
            basename = os.path.basename(full).removesuffix(".jsonl")
            if basename == controller_sid:
                continue  # the controller's own JSONL (would exist at write time anyway, but defensive)
            if basename == sid and sid != controller_sid:
                # If we're running this from a fork, exclude our own JSONL too.
                continue
            candidates.append(full)
candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
for c in candidates:
    print(c)
PY
    ;;

  monitor)
    FORK_JSONL="${2:-}"
    if [ -z "$FORK_JSONL" ] || [ ! -f "$FORK_JSONL" ]; then
      echo "ERROR: usage: $0 monitor <fork-jsonl-path> [--poll <sec>] [--idle-stop <sec>]" >&2
      exit 1
    fi
    POLL=5
    IDLE_STOP=120
    shift 2
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --poll) POLL="$2"; shift 2 ;;
        --idle-stop) IDLE_STOP="$2"; shift 2 ;;
        *) echo "unknown flag: $1" >&2; exit 2 ;;
      esac
    done

    echo "monitoring $FORK_JSONL"
    echo "  poll: ${POLL}s   idle-stop: ${IDLE_STOP}s"
    echo "  exit-on: 'FORK-COMPLETE' marker OR no mtime change for ${IDLE_STOP}s"
    echo

    last_mtime=0
    last_size=0
    idle_seconds=0

    while true; do
      if ! [ -f "$FORK_JSONL" ]; then
        echo "fork JSONL disappeared — stopping monitor"
        exit 1
      fi
      curr_mtime=$(stat -f '%m' "$FORK_JSONL" 2>/dev/null)
      curr_size=$(stat -f '%z' "$FORK_JSONL" 2>/dev/null)

      if [ "$curr_size" != "$last_size" ]; then
        # JSONL grew — extract events since the last poll
        if [ "$last_size" -gt 0 ]; then
          # Print the new lines (everything past the previous size)
          tail -c "+$((last_size + 1))" "$FORK_JSONL" 2>/dev/null | \
            jq -r 'select(.type == "assistant") | .message.content // [] | .[] | select(.type == "text") | .text[0:200]' 2>/dev/null
          # Check for FORK-COMPLETE marker
          if tail -c "+$((last_size + 1))" "$FORK_JSONL" 2>/dev/null | grep -q "FORK-COMPLETE"; then
            echo
            echo "✅ FORK-COMPLETE marker observed — fork signalled completion."
            exit 0
          fi
        fi
        last_size="$curr_size"
        last_mtime="$curr_mtime"
        idle_seconds=0
      else
        idle_seconds=$((idle_seconds + POLL))
        if [ "$idle_seconds" -ge "$IDLE_STOP" ]; then
          echo
          echo "⏸  idle ${idle_seconds}s — fork done or stuck. Stopping monitor."
          exit 0
        fi
      fi
      sleep "$POLL"
    done
    ;;

  detect)
    CONTRACT="${2:-docs-private/.fork-contract.json}"
    if [ ! -f "$CONTRACT" ]; then
      echo "NO_CONTRACT"
      exit 0
    fi
    SID="${CLAUDE_CODE_SESSION_ID:-UNSET}"
    MARKER=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['controller_session_id'])" "$CONTRACT")

    # Subagent disambiguation: if my CLAUDE_CODE_SESSION_ID matches the marker
    # AND there's recent (<60s) activity under any subagents/ subdir, I'm
    # probably a subagent, not the controller. Heuristic; race-prone if
    # multiple subagents fire simultaneously, but the common case (controller
    # idle while subagent runs) works.
    RECENT_SUBAGENT_ACTIVITY=$(find "$HOME/.claude/projects" -path "*/subagents/*.jsonl" -mmin -1 2>/dev/null | head -1)

    if [ "$SID" = "$MARKER" ]; then
      if [ -n "$RECENT_SUBAGENT_ACTIVITY" ]; then
        echo "SUBAGENT"
      else
        echo "ORIGINAL"
      fi
    else
      echo "FORK"
      python3 - "$CONTRACT" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))["fork_contract"]
print(f"task: {c['task']}")
print(f"on completion: {c['completion_signal']}")
print(f"on blocker: {c['abort_signal']}")
PY
    fi
    ;;

  clear)
    CONTRACT="${2:-docs-private/.fork-contract.json}"
    if [ -f "$CONTRACT" ]; then
      rm "$CONTRACT"
      echo "cleared $CONTRACT"
    else
      echo "no contract to clear at $CONTRACT"
    fi
    ;;

  -h|--help|help)
    sed -n '2,42p' "$0" | sed 's/^# \{0,1\}//'
    ;;

  *)
    echo "usage: $0 {write|detect|clear|help} [args...]" >&2
    exit 2
    ;;
esac
