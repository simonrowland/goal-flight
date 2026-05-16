#!/usr/bin/env bash
# install-codex-overrides.sh — register a project as codex-trusted so non-
# interactive `codex exec` dispatches bypass MCP tool approval gates.
#
# Background
# ----------
# When `~/.codex/config.toml` declares an MCP server (e.g. context-mode) with
# per-tool `approval_mode = "approve"`, `codex exec` (non-interactive, no TTY)
# blocks forever on the first MCP tool call — the model invokes the tool,
# codex waits for an approval click that has no surface to arrive on, and the
# dispatch hangs with zero-byte output. This is the ~2/5 silent-stall failure
# mode that goal-flight controllers were hitting.
#
# Fix: codex auto-approves trusted projects' MCP calls when the cwd at exec
# time is inside a project registered as:
#
#   [projects."<ABSOLUTE_PATH>"]
#   trust_level = "trusted"
#
# in `~/.codex/config.toml`. Path matching is prefix-based, so `.claude/
# worktrees/*` and any other subdirectory inherits trust from the project
# root — no per-worktree registration needed.
#
# What this script does
# ---------------------
# 1. Idempotently adds the trust block for <project root> to `~/.codex/config.toml`.
# 2. Optionally mirrors it to `<project>/.codex/config.toml` (self-documenting,
#    survives a future `~/.codex/` rebuild). Defaults on; suppress with --no-project-mirror.
# 3. Echoes a verification block.
#
# Usage
# -----
#   install-codex-overrides.sh                  # detect project root from cwd (git toplevel)
#   install-codex-overrides.sh <path>           # explicit project root
#   install-codex-overrides.sh --check          # report state, write nothing
#   install-codex-overrides.sh --no-project-mirror   # skip writing <project>/.codex/config.toml
#
# Re-running is safe — entries are appended only if not already present.
#
# After install: codex exec dispatches in this project (including from worktrees)
# can use MCP tools without stalling. Recommended dispatch shape (preserves the
# hard wall-clock ceiling — codex v0.130.0 has no --timeout flag):
#
#   timeout --kill-after=10 300 codex exec '<prompt>' > <tail> 2>&1 &

set -euo pipefail

CHECK_ONLY=0
WRITE_PROJECT_MIRROR=1
PROJECT_ROOT=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --check) CHECK_ONLY=1 ;;
    --no-project-mirror) WRITE_PROJECT_MIRROR=0 ;;
    -h|--help) sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) PROJECT_ROOT="$1" ;;
  esac
  shift
done

# Detect project root if not explicit
if [ -z "$PROJECT_ROOT" ]; then
  PROJECT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
fi
PROJECT_ROOT="$(cd "$PROJECT_ROOT" && pwd)"

# --- Path guard (codex reviewer P0 fix) ---
# Reject paths that would trust too much. Trust is prefix-based in codex:
# `[projects."/"]` trusts every cwd; `[projects."/Users/foo"]` trusts the
# entire home directory; single-segment paths like `/tmp` or `/usr` trust
# huge swaths. Require the registration target be a real project — at
# least two path segments under root, AND not be $HOME exactly, AND ideally
# be inside a git repo (unless explicitly running from a non-git target).
case "$PROJECT_ROOT" in
  "/")
    echo "ERROR: refusing to register '/' as codex-trusted." >&2
    echo "  Codex trust is prefix-based; trusting / trusts every cwd." >&2
    exit 2
    ;;
  "$HOME")
    echo "ERROR: refusing to register \$HOME ($HOME) as codex-trusted." >&2
    echo "  Trust on \$HOME applies to every subdirectory; register a specific project." >&2
    exit 2
    ;;
esac
# Count path segments — single-segment paths under root (/tmp, /usr, /etc, etc.)
# are system directories that should not be project-trusted.
SEGMENT_COUNT=$(printf '%s' "$PROJECT_ROOT" | awk -F'/' '{print NF-1}')
if [ "$SEGMENT_COUNT" -lt 2 ]; then
  echo "ERROR: refusing to register '$PROJECT_ROOT' as codex-trusted." >&2
  echo "  Single-segment paths under root are typically system directories." >&2
  echo "  Use a deeper path like '/Users/<you>/Repos/<project>' or '~/Repos/<project>'." >&2
  exit 2
fi
# Bonus check: warn (but don't block) if the path isn't a git repo. Some
# legitimate codex-trusted projects aren't git repos (research dirs, etc.)
# but most are; a missing .git/ is often a sign of a mistake.
if [ ! -d "$PROJECT_ROOT/.git" ] && [ ! -f "$PROJECT_ROOT/.git" ]; then
  echo "WARN: $PROJECT_ROOT is not a git repo. Proceeding (codex trust doesn't require git), but double-check this is the intended target." >&2
fi

USER_CONFIG="$HOME/.codex/config.toml"
PROJECT_CONFIG="$PROJECT_ROOT/.codex/config.toml"
ENTRY_KEY="[projects.\"${PROJECT_ROOT}\"]"

# --- 1. User config ---

if [ ! -f "$USER_CONFIG" ]; then
  if [ "$CHECK_ONLY" -eq 1 ]; then
    echo "CHECK: $USER_CONFIG does not exist."
    exit 1
  fi
  mkdir -p "$(dirname "$USER_CONFIG")"
  : > "$USER_CONFIG"
fi

if grep -qF "$ENTRY_KEY" "$USER_CONFIG"; then
  echo "user config:    already trusts ${PROJECT_ROOT}"
elif [ "$CHECK_ONLY" -eq 1 ]; then
  echo "CHECK: user config does NOT trust ${PROJECT_ROOT}"
  exit 1
else
  # Ensure file ends with newline before appending
  if [ -s "$USER_CONFIG" ] && [ "$(tail -c1 "$USER_CONFIG" | wc -l | tr -d ' ')" != "1" ]; then
    printf '\n' >> "$USER_CONFIG"
  fi
  printf '\n%s\ntrust_level = "trusted"\n' "$ENTRY_KEY" >> "$USER_CONFIG"
  echo "user config:    appended trust entry for ${PROJECT_ROOT}"
fi

# --- 2. Project mirror ---

if [ "$WRITE_PROJECT_MIRROR" -eq 1 ] && [ "$CHECK_ONLY" -eq 0 ]; then
  mkdir -p "$PROJECT_ROOT/.codex"
  if [ ! -f "$PROJECT_CONFIG" ]; then
    cat > "$PROJECT_CONFIG" <<EOF
# Project-scoped codex settings.
#
# The [projects.<abs>] block declares this project as trusted, which bypasses
# [mcp_servers.X.tools.Y].approval_mode = "approve" entries in user config
# that would otherwise block non-interactive codex exec dispatches on the
# first MCP tool call. Worktrees under <project>/.claude/worktrees/ inherit
# trust via path prefix-matching.
#
# Generated by goal-flight's scripts/install-codex-overrides.sh.
# Re-run that script to refresh. This file's absolute paths are machine-
# specific; this script auto-adds .codex/ to the project .gitignore.
approval_policy = "on-request"
sandbox_mode = "workspace-write"

${ENTRY_KEY}
trust_level = "trusted"
EOF
    echo "project config: wrote ${PROJECT_CONFIG}"
  elif grep -qF "$ENTRY_KEY" "$PROJECT_CONFIG"; then
    echo "project config: ${PROJECT_CONFIG} already has trust entry"
  else
    if [ "$(tail -c1 "$PROJECT_CONFIG" | wc -l | tr -d ' ')" != "1" ]; then
      printf '\n' >> "$PROJECT_CONFIG"
    fi
    printf '\n%s\ntrust_level = "trusted"\n' "$ENTRY_KEY" >> "$PROJECT_CONFIG"
    echo "project config: appended trust entry to ${PROJECT_CONFIG}"
  fi

  # Auto-gitignore .codex/ — the project mirror contains absolute paths
  # specific to this machine; they're not portable across clones.
  # Idempotent: recognizes existing entries in any of the common
  # equivalent forms (`.codex`, `.codex/`, `/.codex/`, `.codex/*`) so a
  # second run doesn't duplicate semantically-identical lines.
  GITIGNORE="$PROJECT_ROOT/.gitignore"
  if [ -d "$PROJECT_ROOT/.git" ] || [ -f "$PROJECT_ROOT/.git" ]; then
    already_ignored=0
    if [ -f "$GITIGNORE" ] && grep -qE '^\s*/?\.codex(/(\*)?)?\s*$' "$GITIGNORE"; then
      already_ignored=1
    fi
    if [ "$already_ignored" -eq 0 ]; then
      if [ -f "$GITIGNORE" ]; then
        # Ensure file ends in a newline before append (avoid line concatenation).
        if [ "$(tail -c1 "$GITIGNORE" | wc -l | tr -d ' ')" != "1" ]; then
          printf '\n' >> "$GITIGNORE"
        fi
        printf '\n# Per-machine codex trust mirror (absolute paths, not portable)\n.codex/\n' >> "$GITIGNORE"
      else
        # Fresh .gitignore — no leading blank line.
        printf '# Per-machine codex trust mirror (absolute paths, not portable)\n.codex/\n' > "$GITIGNORE"
      fi
      echo "gitignore:      appended .codex/ to ${GITIGNORE}"
    else
      echo "gitignore:      ${GITIGNORE} already lists .codex/"
    fi
  fi
fi

# --- 3. Verify ---

echo
echo "=== verify ==="
grep -A1 -F "$ENTRY_KEY" "$USER_CONFIG" || echo "MISSING from $USER_CONFIG"
echo
echo "Worktrees under ${PROJECT_ROOT}/.claude/worktrees/ inherit trust via path prefix."
echo "Done."
