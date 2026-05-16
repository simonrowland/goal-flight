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
import json, os, sys, datetime
contract_path, task, sid = sys.argv[1], sys.argv[2], sys.argv[3]
contract = {
  "controller_session_id": sid,
  "marker_written_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
  "fork_contract": {
    "task": task,
    "completion_signal": "git commit + RESUME-NOTES rev bump with FORK-COMPLETE marker",
    "abort_signal": "write docs-private/FORK-BLOCKED-<id>.md and stop without committing",
  },
  "_about": "Written by scripts/self-fork-detect.sh before /fork. After /fork, the new session checks identity via the same script (`detect` mode). The original controller sees ORIGINAL; the fork sees FORK + the task to execute.",
}
with open(contract_path, "w") as f:
  json.dump(contract, f, indent=2)
print(f"wrote {contract_path}")
print(f"  controller_session_id: {contract['controller_session_id']}")
print(f"  task: {contract['fork_contract']['task']}")
PY
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
