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
#   self-fork-detect.sh reply <fork-jsonl-or-sid> '<reply prompt>'
#       Inject a reply into the fork session. Used when the fork emitted
#       FORK-NEED and is waiting for controller/user input. Wraps
#       `claude --resume <sid> --print '<reply>'`.
#
#       Note: `claude -p` is billed at API rates (vs Agent-tool session-
#       billing). Prompt caching makes per-call cost small for short
#       replies — the fork's prior conversation is cached prefix; only
#       the reply turn + response are new tokens.
#
#       Cheaper still: `/rewind` + redo in the controller, or have the
#       user reply in the fork window directly. Pick per context.
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

# The fork has no clean "return value" channel like Agent-tool's task-notification.
# Forks communicate back by EMITTING KEYWORD MARKERS in their assistant text,
# which the controller's `monitor` mode greps from the fork's JSONL. Both ends
# need to agree on the vocabulary — that's what marker_vocabulary captures.
marker_vocabulary = {
    "FORK-STATUS": "intermediate progress; controller logs but keeps monitoring. Example: 'FORK-STATUS: read 3 files, drafting plan'",
    "FORK-RESULT": "structured output the controller should extract; key=value shape works well. Example: 'FORK-RESULT: memo_path=docs-private/MEMO.md'",
    "FORK-NEED":   "fork is blocked on a controller / user decision; monitor exits with code 2 so the controller knows to intervene. Example: 'FORK-NEED: confirm X is in-scope'",
    "FORK-COMPLETE": "fork is done; monitor exits with code 0. Payload is a one-line summary. Example: 'FORK-COMPLETE: queue chunks #4-7 implemented, all tests green'",
    "FORK-BLOCKED": "fork hit an unrecoverable issue, will not continue; monitor exits with code 1. Example: 'FORK-BLOCKED: missing dependency X'",
}

contract = {
  "controller_session_id": sid,
  "marker_written_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
  "jsonl_snapshot": sorted(jsonls_snapshot),
  "fork_contract": {
    "task": task,
    "marker_vocabulary": marker_vocabulary,
    "completion_signal": "emit 'FORK-COMPLETE: <one-line summary>' in your response; commit work + bump RESUME-NOTES rev so the original controller sees the result on /rewind or resume",
    "abort_signal": "emit 'FORK-BLOCKED: <reason>' and stop without committing",
    "intervention_signal": "emit 'FORK-NEED: <question>' if the work hits a decision only the user can make",
    "status_signal": "emit 'FORK-STATUS: <one-line progress update>' periodically so the controller has a heartbeat",
  },
  "_about": "Written by scripts/self-fork-detect.sh before /fork. After /fork, the new session checks identity via `detect` (prints the task + marker vocabulary the fork should emit). Controller can use `find-fork` to locate the new session's JSONL and `monitor` to watch it.",
}
with open(contract_path, "w") as f:
  json.dump(contract, f, indent=2)
print(f"wrote {contract_path}")
print(f"  controller_session_id: {contract['controller_session_id']}")
print(f"  task: {contract['fork_contract']['task']}")
print(f"  jsonl_snapshot: {len(jsonls_snapshot)} files captured")
print(f"  marker vocabulary: {', '.join(marker_vocabulary.keys())}")
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
    echo "  exit-on: 'FORK-COMPLETE' marker OR no size change for ${IDLE_STOP}s"
    echo

    last_size=0
    idle_seconds=0

    # File size is the growth signal. stat(1) differs by platform: BSD/macOS
    # uses `-f %z`, GNU/Linux uses `-c %s`. Detect once against the real file
    # (the probe must yield a pure integer — GNU `stat -f` silently prints
    # non-numeric filesystem info), then fall back to POSIX `wc -c` if neither
    # stat form works. Detecting once keeps the poll loop cheap and portable.
    if stat -f '%z' "$FORK_JSONL" 2>/dev/null | grep -qE '^[0-9]+$'; then
      _statsize() { stat -f '%z' "$1" 2>/dev/null; }
    elif stat -c '%s' "$FORK_JSONL" 2>/dev/null | grep -qE '^[0-9]+$'; then
      _statsize() { stat -c '%s' "$1" 2>/dev/null; }
    else
      _statsize() { wc -c < "$1" 2>/dev/null | tr -d ' '; }
    fi

    while true; do
      if ! [ -f "$FORK_JSONL" ]; then
        echo "fork JSONL disappeared — stopping monitor"
        exit 1
      fi
      curr_size=$(_statsize "$FORK_JSONL")

      if [ "$curr_size" != "$last_size" ]; then
        # JSONL grew — extract events since the last poll
        if [ "$last_size" -gt 0 ]; then
          # Print the new lines (everything past the previous size)
          NEW_CHUNK=$(tail -c "+$((last_size + 1))" "$FORK_JSONL" 2>/dev/null)
          # Extract assistant text and print abbreviated
          echo "$NEW_CHUNK" | jq -r 'select(.type == "assistant") | .message.content // [] | .[] | select(.type == "text") | .text[0:300]' 2>/dev/null

          # Route on FORK-* markers (highest priority terminal markers first).
          if echo "$NEW_CHUNK" | grep -qE "FORK-COMPLETE\b"; then
            echo
            echo "✅ FORK-COMPLETE marker observed — fork signalled completion."
            echo "$NEW_CHUNK" | grep -oE "FORK-COMPLETE[^\"]*" | head -3
            exit 0
          fi
          if echo "$NEW_CHUNK" | grep -qE "FORK-BLOCKED\b"; then
            echo
            echo "❌ FORK-BLOCKED marker observed — fork stopped on unrecoverable issue."
            echo "$NEW_CHUNK" | grep -oE "FORK-BLOCKED[^\"]*" | head -3
            exit 1
          fi
          if echo "$NEW_CHUNK" | grep -qE "FORK-NEED\b"; then
            echo
            echo "🟡 FORK-NEED marker observed — fork needs controller/user intervention."
            echo "$NEW_CHUNK" | grep -oE "FORK-NEED[^\"]*" | head -3
            exit 2
          fi
          # Non-terminal markers: log and keep monitoring.
          if echo "$NEW_CHUNK" | grep -qE "FORK-STATUS\b"; then
            echo "  ↪ status: $(echo "$NEW_CHUNK" | grep -oE "FORK-STATUS[^\"]*" | head -1)"
          fi
          if echo "$NEW_CHUNK" | grep -qE "FORK-RESULT\b"; then
            echo "  ↪ result: $(echo "$NEW_CHUNK" | grep -oE "FORK-RESULT[^\"]*" | head -1)"
          fi
        fi
        last_size="$curr_size"
        idle_seconds=0
      else
        idle_seconds=$((idle_seconds + POLL))
        if [ "$idle_seconds" -ge "$IDLE_STOP" ]; then
          echo
          echo "⏸  idle ${idle_seconds}s — fork done or stuck without emitting a terminal marker."
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
print()
print("You communicate back to the controller by EMITTING KEYWORD MARKERS in your assistant text. There is no clean return channel for forks (no task-notification analog); the controller polls your JSONL for these strings. Emit them literally as shown:")
print()
for marker, desc in c.get("marker_vocabulary", {}).items():
    print(f"  {marker}:  {desc}")
print()
print(f"completion signal: {c['completion_signal']}")
print(f"abort signal:      {c['abort_signal']}")
print(f"intervention:      {c.get('intervention_signal', '(emit FORK-NEED if the work hits a controller-only decision)')}")
print(f"status (heartbeat): {c.get('status_signal', '(emit FORK-STATUS periodically)')}")
PY
    fi
    ;;

  reply)
    TARGET="${2:-}"
    REPLY="${3:-}"
    if [ -z "$TARGET" ] || [ -z "$REPLY" ]; then
      echo "ERROR: usage: $0 reply <fork-jsonl-or-sid> '<reply prompt>'" >&2
      exit 1
    fi
    # Extract session ID — accept either a JSONL path or a bare UUID.
    case "$TARGET" in
      *.jsonl)
        FORK_SID=$(basename "$TARGET" .jsonl)
        ;;
      *)
        FORK_SID="$TARGET"
        ;;
    esac
    # Validate UUID-ish shape (8-4-4-4-12 hex segments).
    if ! echo "$FORK_SID" | grep -qE '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'; then
      echo "ERROR: fork session ID doesn't match UUID shape: $FORK_SID" >&2
      echo "  pass either the fork's JSONL path or a bare session-id UUID." >&2
      exit 1
    fi
    echo >&2 "injecting reply into fork session $FORK_SID (Note: claude -p is billed at API rates)..."
    claude --resume "$FORK_SID" --print "$REPLY"
    rc=$?
    echo >&2
    echo >&2 "reply injected. The fork's JSONL has grown with the reply turn + response."
    echo >&2 "Re-invoke 'self-fork-detect.sh monitor <fork-jsonl>' to keep watching."
    exit $rc
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
