#!/bin/sh

# Claude Code SessionStart hook. Conservatively injects the event-wake-first
# controller contract and its in-session crash-recovery fallback when this repo
# has active or recent work.

resolve_repo_root() {
  hook_src=${1:-$0}
  hops=0
  while [ -L "$hook_src" ] && [ "$hops" -lt 40 ]; do
    hops=$((hops + 1))
    link_target=$(readlink "$hook_src" 2>/dev/null || true)
    [ -n "$link_target" ] || break
    case "$link_target" in
      /*) hook_src=$link_target ;;
      *) hook_src=$(cd "$(dirname "$hook_src")" 2>/dev/null && pwd)/$link_target ;;
    esac
  done
  cd "$(dirname "$hook_src")/../.." 2>/dev/null && pwd
}

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

run_self_test() {
  repo_root=$(resolve_repo_root "$0") || fail "repo root"
  tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/goalflight-watchdog-hook.XXXXXX")
  trap 'rm -rf "$tmp_dir"' EXIT INT TERM
  status_dir="$tmp_dir/dispatch"
  mkdir -p "$status_dir"
  export GOALFLIGHT_TASK_STORE_DIR="$tmp_dir/task-store"
  export GOALFLIGHT_JOURNAL_DIR="$tmp_dir/journal"
  export GOALFLIGHT_MESSAGES_DIR="$tmp_dir/messages"
  export GOAL_FLIGHT_PIDFILE_DIR="$tmp_dir/pidfiles"
  export GOALFLIGHT_CAPACITY_CONF=/dev/null
  export GOALFLIGHT_PROCESS_ROLE=dashboard
  export GOALFLIGHT_TEST_MODE=1
  payload='{"hook_event_name":"SessionStart","source":"startup","cwd":"'"$repo_root"'"}'
  hook_shell=${GOALFLIGHT_WATCHDOG_SELFTEST_SHELL:-/bin/sh}

  out=$(env \
    GOALFLIGHT_WATCHDOG_STATUS_GLOB="$status_dir/*.status.json" \
    GOALFLIGHT_WATCHDOG_RESUME_GLOB="$tmp_dir/RESUME-NOTES-*.md" \
    GOALFLIGHT_WATCHDOG_SKIP_STATUS_SCRIPT=1 \
    "$hook_shell" "$0" <<EOF
$payload
EOF
  )
  [ -z "$out" ] || fail "empty state should be silent"

  PYTHONPATH="$repo_root/scripts:$repo_root${PYTHONPATH:+:$PYTHONPATH}" \
    python3 - "$repo_root" <<'PY' || fail "seed journal activity"
import sys
import goalflight_journal

authority = goalflight_journal.open_or_create_journal(sys.argv[1])
result = authority.prepare_attempt("self-test")
assert result.committed
PY

  i=0
  while [ "$i" -lt 12 ]; do
    cat > "$status_dir/stale-$i.status.json" <<EOF
{"state":"complete","project_root":"$repo_root","dispatch_id":"stale-$i"}
EOF
    i=$((i + 1))
  done
  cat > "$status_dir/active.status.json" <<EOF
{"state":"running","project_root":"$repo_root","dispatch_id":"self-test"}
EOF
  out=$(env \
    GOALFLIGHT_WATCHDOG_STATUS_GLOB="$status_dir/*.status.json" \
    GOALFLIGHT_WATCHDOG_STATUS_FILE_CAP=4 \
    GOALFLIGHT_WATCHDOG_RESUME_GLOB="$tmp_dir/RESUME-NOTES-*.md" \
    GOALFLIGHT_WATCHDOG_SKIP_STATUS_SCRIPT=1 \
    "$hook_shell" "$0" <<EOF
$payload
EOF
  )
  printf '%s' "$out" | python3 -c 'import json,sys
d=json.load(sys.stdin)
ctx=d["hookSpecificOutput"]["additionalContext"]
assert d["hookSpecificOutput"]["hookEventName"] == "SessionStart"
assert "CronList" in ctx and "CronCreate" in ctx
assert "goalflight-watchdog-prompt.md" in ctx
assert "ARM THE EVENT WAKE FIRST" in ctx
assert "goalflight_messages.py listen" not in ctx
assert "do not arm a direct wake component" in ctx
assert "goalflight_status.py --wait" in ctx
assert "returned `session.lease_nonce`" in ctx
assert "crash-recovery fallback only" in ctx
assert "`7 * * * *`" in ctx
assert ctx.index("ARM THE EVENT WAKE FIRST") < ctx.index("CronList")
assert "15-min" not in ctx
assert "Then poll" not in ctx
' || fail "running dispatch should inject"

  python3 - "$repo_root/templates/goalflight-watchdog-prompt.md" <<'PY' || fail "canonical prompt contract"
import pathlib
import sys

text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
assert "event-wait-live" in text
assert "do nothing else" in text
assert "listener coverage row" in text
assert "goalflight_status.py --wait <ids>" in text
assert "crash-recovery fallback only" in text
assert "Schedule: `7 * * * *` (hourly)" in text
assert "After every new dispatch, arm or retain the event wait" in text
assert "Poll files; do not trust background notifications." not in text
assert "Re-arm this watchdog after any new dispatch." not in text
assert "7,22,37,52 * * * *" not in text
PY

  out=$(env \
    GOALFLIGHT_WATCHDOG_STATUS_GLOB="$status_dir/*.status.json" \
    GOALFLIGHT_WATCHDOG_RESUME_GLOB="$tmp_dir/RESUME-NOTES-*.md" \
    GOALFLIGHT_WATCHDOG_SKIP_STATUS_SCRIPT=1 \
    "$hook_shell" "$0" <<EOF
{"hook_event_name":"SessionStart","source":"startup","cwd":"$tmp_dir/outside"}
EOF
  )
  [ -z "$out" ] || fail "out-of-scope cwd should be silent"

  rm -f "$status_dir"/*.status.json
  : > "$tmp_dir/RESUME-NOTES-2026-05-31.md"
  out=$(env \
    GOALFLIGHT_WATCHDOG_STATUS_GLOB="$status_dir/*.status.json" \
    GOALFLIGHT_WATCHDOG_RESUME_GLOB="$tmp_dir/RESUME-NOTES-*.md" \
    GOALFLIGHT_WATCHDOG_SKIP_STATUS_SCRIPT=1 \
    "$hook_shell" "$0" <<EOF
$payload
EOF
  )
  printf '%s' "$out" | python3 -c 'import json,sys
d=json.load(sys.stdin)
assert "additionalContext" in d["hookSpecificOutput"]
' || fail "recent resume note should inject"

  fail_open_stdout="$tmp_dir/fail-open.stdout"
  fail_open_stderr="$tmp_dir/fail-open.stderr"
  empty_path="$tmp_dir/empty-path"
  mkdir -p "$empty_path"
  env \
    PATH="$empty_path" \
    GOALFLIGHT_WATCHDOG_STATUS_GLOB="$status_dir/*.status.json" \
    GOALFLIGHT_WATCHDOG_RESUME_GLOB="$tmp_dir/no-resume-*.md" \
    GOALFLIGHT_WATCHDOG_SKIP_STATUS_SCRIPT=1 \
    "$hook_shell" "$0" >"$fail_open_stdout" 2>"$fail_open_stderr" <<EOF
$payload
EOF
  code=$?
  [ "$code" -eq 0 ] || fail "missing python/tools should exit 0"
  [ ! -s "$fail_open_stdout" ] || fail "missing python/tools should be silent"
  [ ! -s "$fail_open_stderr" ] || fail "missing python/tools should not leak stderr"

  malformed_stdout="$tmp_dir/malformed.stdout"
  malformed_stderr="$tmp_dir/malformed.stderr"
  env \
    GOALFLIGHT_WATCHDOG_STATUS_GLOB="$status_dir/*.status.json" \
    GOALFLIGHT_WATCHDOG_RESUME_GLOB="$tmp_dir/no-resume-*.md" \
    GOALFLIGHT_WATCHDOG_SKIP_STATUS_SCRIPT=1 \
    "$hook_shell" "$0" >"$malformed_stdout" 2>"$malformed_stderr" <<EOF
{"hook_event_name":
EOF
  code=$?
  [ "$code" -eq 0 ] || fail "malformed input should exit 0"
  [ ! -s "$malformed_stdout" ] || fail "malformed input should be silent"
  [ ! -s "$malformed_stderr" ] || fail "malformed input should not leak stderr"

  printf 'PASS: goalflight-session-start-watchdog self-test\n'
}

case "${1:-}" in
  --self-test)
    run_self_test
    exit $?
    ;;
esac

main() {
  input_json=$(cat 2>/dev/null) || input_json=""
  plugin_root=$(resolve_repo_root "$0" 2>/dev/null || true)
  [ -n "$plugin_root" ] || return 0
  [ "$plugin_root" != "/" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0

  export GOALFLIGHT_HOOK_INPUT="$input_json"
  export GOALFLIGHT_PLUGIN_ROOT="$plugin_root"
  export GOALFLIGHT_WATCHDOG_RECENT_SECONDS="${GOALFLIGHT_WATCHDOG_RECENT_SECONDS:-172800}"

  python3 - <<'PY' 2>/dev/null || true
import glob
import json
import os
import subprocess
import sys
import time


def load_payload() -> dict:
    raw = os.environ.get("GOALFLIGHT_HOOK_INPUT") or "{}"
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def under(child: str, parent: str) -> bool:
    if not child or not parent:
        return False
    try:
        child_real = os.path.realpath(child)
        parent_real = os.path.realpath(parent)
        return os.path.commonpath([child_real, parent_real]) == parent_real
    except Exception:
        return False


def find_goalflight_root(cwd, plugin_root):
    start = os.path.realpath(cwd or "")
    if start and os.path.isdir(start):
        cursor = start
        while True:
            if (
                os.path.isfile(os.path.join(cursor, "SKILL.md"))
                and os.path.isfile(os.path.join(cursor, "scripts", "goalflight_session_status.py"))
            ):
                return cursor
            parent = os.path.dirname(cursor)
            if parent == cursor:
                break
            cursor = parent
    if under(cwd, plugin_root):
        return os.path.realpath(plugin_root)
    return None


def has_recent_resume_note() -> bool:
    try:
        recent_seconds = int(os.environ.get("GOALFLIGHT_WATCHDOG_RECENT_SECONDS", "172800"))
    except ValueError:
        recent_seconds = 172800
    if recent_seconds <= 0:
        return False
    cutoff = time.time() - recent_seconds
    resume_glob = os.environ.get("GOALFLIGHT_WATCHDOG_RESUME_GLOB") or os.path.join(
        os.environ["GOALFLIGHT_REPO_ROOT"], "docs-private", "RESUME-NOTES-*.md"
    )
    for path in glob.glob(resume_glob):
        try:
            if os.path.getmtime(path) >= cutoff:
                return True
        except OSError:
            continue
    return False


def session_status_active(repo_root: str) -> bool:
    if os.environ.get("GOALFLIGHT_WATCHDOG_SKIP_STATUS_SCRIPT") == "1":
        return False
    script = os.environ.get("GOALFLIGHT_WATCHDOG_STATUS_SCRIPT") or os.path.join(
        repo_root, "scripts", "goalflight_session_status.py"
    )
    if not script or not os.path.isfile(script):
        return False
    try:
        result = subprocess.run(
            [sys.executable, script, "--text"],
            cwd=repo_root,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            # SessionStart fail-open wall: observability must never block work.
            # Intentionally tighter than JOURNAL_WRITER_RETRY_BUDGET_S (5s),
            # which is the in-process durable-writer contract, not this hook.
            timeout=3,
            check=False,
        )
    except Exception:
        return False
    return result.returncode == 0 and result.stdout.lower().startswith("active goal-flight session")


def claim_controller_entry(repo_root: str, cwd: str) -> dict:
    script = os.path.join(repo_root, "scripts", "goalflight_session_status.py")
    try:
        result = subprocess.run(
            [
                sys.executable,
                script,
                "--project-root",
                cwd,
                "--controller-startup",
                "--controller-pid-from-ancestry",
            ],
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            # Same 3s SessionStart wall as session_status_active. A 5s writer
            # budget cannot complete inside this kill; fail-open via timeout
            # rather than stretching SessionStart to the in-process writer bound.
            timeout=3,
            check=False,
        )
        payload = json.loads(result.stdout or "{}")
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def controller_wake_instruction(repo_root: str, claim_result: dict) -> str:
    if (
        claim_result.get("claimed")
        and claim_result.get("wake_supervisor") == "running"
    ):
        return ""
    depth = claim_result.get("listener_depth")
    if not claim_result.get("claimed") or not isinstance(depth, dict):
        return (
            "Wake ownership is not established; do not arm a direct wake component "
            "until status establishes whether `supervise` is running."
        )
    try:
        sys.path.insert(0, repo_root)
        sys.path.insert(0, os.path.join(repo_root, "scripts"))
        import goalflight_wake

        state = str(depth.get("supervisor") or goalflight_wake.SUPERVISOR_UNKNOWN)
        command = str(depth.get("command") or "")
        action = goalflight_wake.supervisor_operator_action(
            state,
            component_command=(
                command if state == goalflight_wake.SUPERVISOR_ABSENT else None
            ),
            supervise_command=(
                command if state == goalflight_wake.SUPERVISOR_RUNNING else None
            ),
        )
        return str(action["instruction"])
    except Exception:
        return (
            "Wake ownership could not be determined; do not arm a direct wake "
            "component until status establishes whether `supervise` is running."
        )


def journal_activity(repo_root: str, cwd: str) -> bool:
    try:
        sys.path.insert(0, repo_root)
        sys.path.insert(0, os.path.join(repo_root, "scripts"))
        import goalflight_journal
        import goalflight_task

        root = goalflight_task.resolve_project_root(cwd)
        # Peek-only: must not take the write lock or inherit the 5s writer
        # budget. Open retries default to JOURNAL_OPEN_RETRY_BUDGET_S (75s);
        # cap them to the same 3s SessionStart wall as the subprocess calls.
        # Busy stays at the 1.0s reader default. Fail-open prefers a fast
        # False over a stall.
        authority = goalflight_journal.Journal.open_reader(
            root,
            open_retry_budget_s=3.0,
        )
        if authority.attention_items():
            return True
        return bool(
            authority.read_all(
                """SELECT 1 FROM dispatch_attempts
                   WHERE project_root = ? AND lifecycle_state IN ('PREPARED', 'STARTING', 'RUNNING')
                   LIMIT 1""",
                (str(root),),
            )
        )
    except Exception:
        return False


def main() -> None:
    payload = load_payload()
    if not payload:
        return
    cwd = payload.get("cwd") or os.environ.get("CLAUDE_CODE_CWD") or os.environ.get("PWD") or ""
    plugin_root = os.environ["GOALFLIGHT_PLUGIN_ROOT"]
    repo_root = find_goalflight_root(str(cwd), plugin_root)
    if not repo_root:
        return
    os.environ["GOALFLIGHT_REPO_ROOT"] = repo_root
    claim_result = claim_controller_entry(repo_root, str(cwd))

    if not (journal_activity(repo_root, str(cwd)) or has_recent_resume_note() or session_status_active(repo_root)):
        return

    prompt_file = os.environ.get("GOALFLIGHT_WATCHDOG_PROMPT_FILE")
    if not prompt_file:
        repo_prompt = os.path.join(repo_root, "templates", "goalflight-watchdog-prompt.md")
        plugin_prompt = os.path.join(plugin_root, "templates", "goalflight-watchdog-prompt.md")
        prompt_file = repo_prompt if os.path.isfile(repo_prompt) else plugin_prompt
    wake_instruction = controller_wake_instruction(repo_root, claim_result)
    depth = claim_result.get("listener_depth")
    wake_state = (
        str(claim_result.get("wake_supervisor") or "")
        or (
            str(depth.get("supervisor") or "")
            if isinstance(depth, dict)
            else ""
        )
    )
    if wake_state == "running":
        wake_preamble = (
            "A live `supervise` process already owns this controller generation's "
            "event wake; no controller wake action is required. "
        )
    elif wake_state == "unknown":
        wake_preamble = (
            "RESOLVE EVENT WAKE OWNERSHIP FIRST before arming any direct wake "
            "component: "
        )
    else:
        wake_preamble = (
            "ARM THE EVENT WAKE FIRST as a background task per "
            "`protocols/dispatch-routing.md` and `commands/execute.md` — "
            "prefer ONE `goalflight_messages.py supervise` process armed "
            "through the host's persistent monitor (Claude Code: the Monitor "
            "tool with `persistent: true`, NO timeout — never a bounded "
            "monitor, never shell `&`), stopping any old direct listeners "
            "first; arm a bare component only where no persistent monitor "
            "exists: "
        )
    claimed_instruction = (
        f"For a claimed controller: {wake_instruction} "
        if wake_instruction
        else ""
    )
    context = (
        "An active goal-flight run was detected on this session start. "
        f"{wake_preamble}"
        "the SessionStart hook already attempted a role-aware lease claim; inspect its result "
        f"({json.dumps(claim_result, sort_keys=True)}). Carry the returned `session.lease_nonce`. "
        f"{claimed_instruction}An "
        "unclaimed fixed-set controller runs the printed `goalflight_status.py --wait <ids>` "
        "command. Do not block the controller turn on either wait. CONTINUE IN-SKILL: re-invoke "
        "`/goal-flight resume` (this reloads SKILL.md fresh "
        "from disk) and keep dispatching workers with review-before-commit; do NOT fall "
        "back to default inline editing or abandon the in-flight task. The cron is a "
        "crash-recovery fallback only: run `CronList`; if the hourly self-suspending fallback "
        "is absent, create it once via `CronCreate` with schedule `7 * * * *` and the canonical "
        f"watchdog prompt at `{prompt_file}`. After every new dispatch, arm or retain the event "
        "wait; never create or re-arm a per-dispatch cron. Resume durable state per the newest "
        "`docs-private/RESUME-NOTES-*.md`. Context-mode MCP is fine for your own context discipline."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }, separators=(",", ":")))


try:
    main()
except Exception:
    pass
PY
}

main "$@" 2>/dev/null || true
exit 0
