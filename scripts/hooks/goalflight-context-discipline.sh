#!/usr/bin/env bash
set -euo pipefail

# Claude Code PreToolUse hook for Goal Flight context discipline.
# Reads a tool-call JSON object from stdin or a --dry-run fixture path and
# emits a compact block decision JSON object on stdout.
#
# SCOPE: this hook fires globally (every Claude Code session, every project)
# when symlinked into ~/.claude/hooks/ and registered in settings.json.
# The block rules (Read >5KB, heredoc Bash) would break unrelated sessions
# (Cowork, other repos) if applied globally. The scope gate below makes the
# hook a no-op outside the goal-flight repo unless GOALFLIGHT_HOOKS_FORCE=1
# is set (test/dev override).
#
# Detection precedence for the session's working directory:
#   1. payload.cwd field from the PreToolUse JSON (if Claude Code provides it)
#   2. CLAUDE_CODE_CWD env var (if Claude Code exports it)
#   3. PWD env var (best-effort fallback)
# The goal-flight repo root is derived from THIS script's own real location
# (resolving the ~/.claude/hooks symlink back to <repo>/scripts/hooks/), so no
# operator-specific path is embedded. Enforce only when the working directory
# is under that root; otherwise no-op. If the root cannot be derived, fail open
# (never block an unrelated session). NOTE: this matches a goal-flight-repo cwd
# only; firing for goal-flight-on-any-project (an active-controller marker) is
# the Wave-A follow-up.

dry_run=0
if [[ "${1:-}" == "--dry-run" ]]; then
  dry_run=1
  shift
fi

if [[ $# -gt 0 ]]; then
  input_json=$(cat "$1")
else
  input_json=$(cat)
fi

# Derive the goal-flight repo root from this script's own real path, resolving
# the ~/.claude/hooks symlink back to <repo>/scripts/hooks/. Scope-gate and the
# spawn_task active-run check both use this. Every step is fail-open-safe.
_hook_src="${BASH_SOURCE[0]:-$0}"
_hook_hops=0
while [ -L "$_hook_src" ] && [ "$_hook_hops" -lt 40 ]; do
  _hook_hops=$((_hook_hops + 1))
  _hook_link="$(readlink "$_hook_src" || true)"
  [ -n "$_hook_link" ] || break
  case "$_hook_link" in
    /*) _hook_src="$_hook_link" ;;
    *)  _hook_src="$(cd "$(dirname "$_hook_src")" 2>/dev/null && pwd || true)/$_hook_link" ;;
  esac
done
goalflight_root="$(cd "$(dirname "$_hook_src")/../.." 2>/dev/null && pwd || true)"

# Scope gate (skipped under --dry-run so test fixtures keep working).
if [[ "$dry_run" -eq 0 ]]; then
  payload_cwd=$(printf '%s' "$input_json" | python3 -c 'import json,sys
try:
    d = json.loads(sys.stdin.read() or "{}")
    print(d.get("cwd") or "")
except Exception:
    print("")' 2>/dev/null || true)
  effective_cwd="${payload_cwd:-${CLAUDE_CODE_CWD:-${PWD:-}}}"

  # Enforce only when cwd is under a sanely-derived root. An empty or "/" root
  # (derivation failed) must NOT become a match-everything prefix -> fail open.
  in_scope=0
  if [[ -n "${goalflight_root}" && "${goalflight_root}" != "/" ]]; then
    case "${effective_cwd}" in
      "${goalflight_root}"*) in_scope=1 ;;
    esac
  fi
  if [[ "$in_scope" -ne 1 && "${GOALFLIGHT_HOOKS_FORCE:-}" != "1" ]]; then
    echo '{"block":false}'
    exit 0
  fi
fi

python3 -c '
import json
import os
import re
import subprocess
import sys

READ_MESSAGE = (
    "Read of file >5KB without GOALFLIGHT_RECON_OK=1: use Agent "
    "(Explore for read-only recon) with a defined prompt instead. "
    "To override for this specific call: GOALFLIGHT_RECON_OK=1 <cmd>."
)
HEREDOC_MESSAGE = (
    "Bash heredoc body >50 lines: write the body to a file under scripts/ "
    "or docs-private/ and invoke the file. For commit messages: git commit "
    "-F <msg-file> -- <files>. Override: GOALFLIGHT_HEREDOC_OK=1 <cmd>."
)
PYTHON_HEREDOC_MESSAGE = (
    "Bash python heredoc body >=30 lines: extract as a helper script under "
    "scripts/ and invoke the file. Override: GOALFLIGHT_HEREDOC_OK=1 <cmd>."
)
READY_WARNING = (
    "WARN: Agent/Task/Explore dispatch prompt lacks READY: literal; "
    "file-backed return contract may be lost."
)
CHIP_MESSAGE = (
    "spawn_task chips are disabled during an active goal-flight run. "
    "Route out-of-scope findings to the goal-queue Backlog "
    "(docs-private/goal-queue-*.md via /goal-flight goal); a worker surfaces "
    "them in its RESULT for the controller to harvest. Chips strand worktrees "
    "and orphan work from the ledger/resume plane. Override for a genuine "
    "user-interaction task with no active run: GOALFLIGHT_CHIP_OK=1."
)

HOOK_REPO_ROOT = sys.argv[2] if len(sys.argv) > 2 else ""
SESSION_STATUS_SCRIPT = (
    os.environ.get("GOALFLIGHT_SESSION_STATUS_SCRIPT")
    or os.path.join(HOOK_REPO_ROOT, "scripts", "goalflight_session_status.py")
)
ACTIVE_STATUS_PREFIX = "active goal-flight session"


def emit(obj):
    print(json.dumps(obj, separators=(",", ":")))


def allow(**extra):
    obj = {"block": False}
    obj.update(extra)
    emit(obj)


def block(message):
    emit({"block": True, "message": message})


def as_dict(value):
    return value if isinstance(value, dict) else {}


def as_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def tool_name(payload):
    candidates = [
        payload.get("tool_name"),
        payload.get("tool"),
        payload.get("name"),
    ]
    for candidate in candidates:
        if isinstance(candidate, dict):
            candidate = candidate.get("name")
        if isinstance(candidate, str) and candidate:
            return candidate
    return ""


def tool_input(payload):
    for key in ("tool_input", "input", "arguments"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {}


def active_goalflight_run():
    project_root = os.environ.get("GOALFLIGHT_HOOK_PROJECT_ROOT") or HOOK_REPO_ROOT
    if not project_root or not SESSION_STATUS_SCRIPT:
        return False
    try:
        proc = subprocess.run(
            [
                sys.executable,
                SESSION_STATUS_SCRIPT,
                "--text",
                "--project-root",
                project_root,
            ],
            cwd=HOOK_REPO_ROOT or None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    return (proc.stdout or "").lstrip().startswith(ACTIVE_STATUS_PREFIX)


OPEN_RE = re.compile(r"<<-?\s*([\"'\'']?)([A-Za-z_][A-Za-z0-9_]*)\1\b")
PY_RE = re.compile(r"\bpython(?:3(?:\.\d+)?)?\b.*<<-?\s*([\"'\'']?)([A-Za-z_][A-Za-z0-9_]*)\1\b")


def heredocs(command):
    lines = command.splitlines()
    found = []
    for idx, line in enumerate(lines):
        for match in OPEN_RE.finditer(line):
            marker = match.group(2)
            body_lines = 0
            end_idx = None
            for cursor in range(idx + 1, len(lines)):
                if lines[cursor].strip() == marker:
                    end_idx = cursor
                    break
                body_lines += 1
            if end_idx is None:
                continue
            found.append({
                "marker": marker,
                "lines": body_lines,
                "python": bool(PY_RE.search(line)),
            })
    return found


try:
    payload = json.loads(sys.stdin.read() or "{}")
except json.JSONDecodeError as exc:
    print(f"WARN: invalid hook JSON: {exc}", file=sys.stderr)
    allow()
    raise SystemExit(0)

payload = as_dict(payload)
name = tool_name(payload)
inp = tool_input(payload)

if name == "mcp__ccd_session__spawn_task" and os.environ.get("GOALFLIGHT_CHIP_OK") != "1":
    if active_goalflight_run():
        block(CHIP_MESSAGE)
        raise SystemExit(0)

if name == "Read" and os.environ.get("GOALFLIGHT_RECON_OK") != "1":
    path = inp.get("file_path") or inp.get("path") or inp.get("file")
    if isinstance(path, str) and path:
        try:
            if os.path.isfile(path) and os.path.getsize(path) > 5 * 1024:
                block(READ_MESSAGE)
                raise SystemExit(0)
        except OSError:
            pass

if name == "Bash" and os.environ.get("GOALFLIGHT_HEREDOC_OK") != "1":
    command = as_text(inp.get("command"))
    docs = heredocs(command)
    if any(item["python"] and item["lines"] >= 30 for item in docs):
        block(PYTHON_HEREDOC_MESSAGE)
        raise SystemExit(0)
    if any(item["lines"] > 50 for item in docs):
        block(HEREDOC_MESSAGE)
        raise SystemExit(0)

if name in {"Agent", "Task", "Explore"}:
    prompt_text = as_text(inp)
    if "READY:" not in prompt_text:
        print(READY_WARNING, file=sys.stderr)
        allow(warning=READY_WARNING)
        raise SystemExit(0)

allow()
' "$dry_run" "$goalflight_root" <<<"$input_json"
