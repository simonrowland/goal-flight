#!/bin/sh

# Claude Code SessionStart hook. Conservatively injects a reminder to re-arm the
# in-session Goal Flight watchdog when this repo has active or recent work.

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
' || fail "running dispatch should inject"

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
import heapq
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


def status_belongs_to_repo(data: dict, repo_root: str) -> bool:
    for key in ("project_root", "worker_cwd"):
        value = data.get(key)
        if isinstance(value, str) and under(value, repo_root):
            return True
    return False


def bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def bounded_float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def newest_status_paths(status_glob: str, cap: int, deadline: float):
    newest = []
    for path in glob.iglob(status_glob):
        if time.monotonic() >= deadline:
            break
        try:
            item = (os.path.getmtime(path), path)
        except OSError:
            continue
        if len(newest) < cap:
            heapq.heappush(newest, item)
        elif item > newest[0]:
            heapq.heapreplace(newest, item)
    return [path for _, path in sorted(newest, reverse=True)]


def has_running_dispatch(repo_root: str) -> bool:
    status_glob = os.environ.get("GOALFLIGHT_WATCHDOG_STATUS_GLOB") or "/tmp/goal-flight-*/dispatch/*.status.json"
    file_cap = bounded_int_env("GOALFLIGHT_WATCHDOG_STATUS_FILE_CAP", 64, 1, 4096)
    probe_seconds = bounded_float_env("GOALFLIGHT_WATCHDOG_PROBE_SECONDS", 0.75, 0.05, 4.0)
    deadline = time.monotonic() + probe_seconds
    for path in newest_status_paths(status_glob, file_cap, deadline):
        if time.monotonic() >= deadline:
            break
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                data = json.load(handle)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        state = str(data.get("state") or "").casefold()
        if state == "running" and status_belongs_to_repo(data, repo_root):
            return True
    return False


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
            timeout=3,
            check=False,
        )
    except Exception:
        return False
    return result.returncode == 0 and result.stdout.lower().startswith("active goal-flight session")


def main() -> None:
    payload = load_payload()
    cwd = payload.get("cwd") or os.environ.get("CLAUDE_CODE_CWD") or os.environ.get("PWD") or ""
    plugin_root = os.environ["GOALFLIGHT_PLUGIN_ROOT"]
    repo_root = find_goalflight_root(str(cwd), plugin_root)
    if not repo_root:
        return
    os.environ["GOALFLIGHT_REPO_ROOT"] = repo_root

    if not (has_running_dispatch(repo_root) or has_recent_resume_note() or session_status_active(repo_root)):
        return

    prompt_file = os.environ.get("GOALFLIGHT_WATCHDOG_PROMPT_FILE")
    if not prompt_file:
        repo_prompt = os.path.join(repo_root, "templates", "goalflight-watchdog-prompt.md")
        plugin_prompt = os.path.join(plugin_root, "templates", "goalflight-watchdog-prompt.md")
        prompt_file = repo_prompt if os.path.isfile(repo_prompt) else plugin_prompt
    context = (
        "An active goal-flight run was detected on this session start. "
        "CONTINUE IN-SKILL: re-invoke `/goal-flight resume` (this reloads SKILL.md fresh "
        "from disk) and keep dispatching workers with review-before-commit; do NOT fall "
        "back to default inline editing or abandon the in-flight task. "
        "Re-arm the watchdog: run `CronList`; if the 15-min self-suspending watchdog cron "
        "is absent, re-create it via `CronCreate` with schedule `7,22,37,52 * * * *` and "
        f"the canonical watchdog prompt at `{prompt_file}`. Then poll newest bounded "
        "in-flight dispatch status JSON files under "
        "`/tmp/goal-flight-*/dispatch/*.status.json` and resume per the newest "
        "`docs-private/RESUME-NOTES-*.md`. Do not use context-mode MCP."
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
