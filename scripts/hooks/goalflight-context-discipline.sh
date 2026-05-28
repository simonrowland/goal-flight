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
# Detection precedence:
#   1. payload.cwd field from the PreToolUse JSON (if Claude Code provides it)
#   2. CLAUDE_CODE_CWD env var (if Claude Code exports it)
#   3. PWD env var (best-effort fallback)
# Match: prefix `/Users/simonrowland/Repos/goal-flight`. This is a known
# hardcoded path; portable detection (probe for goal-flight repo marker)
# is queued as Wave-A follow-up.

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

# Scope gate (skipped under --dry-run so test fixtures keep working).
if [[ "$dry_run" -eq 0 ]]; then
  goalflight_root="/Users/simonrowland/Repos/goal-flight"
  payload_cwd=$(printf '%s' "$input_json" | python3 -c 'import json,sys
try:
    d = json.loads(sys.stdin.read() or "{}")
    print(d.get("cwd") or "")
except Exception:
    print("")' 2>/dev/null || true)
  effective_cwd="${payload_cwd:-${CLAUDE_CODE_CWD:-${PWD:-}}}"
  case "${effective_cwd}" in
    "${goalflight_root}"*) ;;
    *)
      if [[ "${GOALFLIGHT_HOOKS_FORCE:-}" != "1" ]]; then
        echo '{"block":false}'
        exit 0
      fi
      ;;
  esac
fi

python3 -c '
import json
import os
import re
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
' "$dry_run" <<<"$input_json"
