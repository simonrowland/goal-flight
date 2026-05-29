# Foreground-duration hook protocol

Per the Hard Invariant "Background anything expected to run longer than
10 seconds," the controller MUST `run_in_background: true` for Bash
calls that match known-slow command families. Foreground calls block
the user's terminal for the full duration; a single 180s `./tests/run.sh`
in foreground = 3 minutes of dead terminal.

Memory reference: `feedback_executor_background_dispatch` +
`feedback_environmental_design_for_context_discipline`.
Source data: `docs-private/research/2026-05-28-r2b-protocol-lists/findings.md`.

## Command families

| Prefix pattern | Typical duration | Action | Sample obs |
|---|---|---|---|
| `find` (filesystem scan) | 2–30s | **force-background** | 32 foreground violations |
| `./tests/run.sh` / `./test*` | 10–180s | **force-background** | 25 foreground violations (incl. one 180s timeout) |
| `grep -r` (recursive) | 1–15s | **warn** | 10 foreground (often under 10s; warn-only) |
| `pytest` | 5–20s+ | **warn** | varies by suite size |
| `git fetch` / `git pull` / `git clone` | 5–60s | **warn** | network-dependent |
| `npm test` / `cargo test` / `npm install` | 10–300s | **force-background** | not in sample but well-known slow |
| `curl` / `wget` / `gh api` (network) | 1–30s | **warn** | depends on endpoint + payload |
| `codex exec` / `grok -p` / dispatch invocations | 60s+ | **force-background** | always — these are worker dispatches |

**force-background**: PreToolUse hook should rewrite the tool call to
add `run_in_background: true` (or block the call with a clear redirect
message if rewriting is not possible).

**warn**: emit a STDERR warning; do not block. The controller may have
a legitimate reason for foreground (e.g., expected sub-1s + needs
immediate result).

## Enforcement

- **Floor (hard)**: PreToolUse Bash hook checks the command against
  the prefix patterns above. force-background prefixes get either
  rewritten to background or hard-blocked (preferring rewrite; if the
  harness's PreToolUse contract doesn't allow rewrites, block with the
  warn-shape message and let the controller re-emit with
  `run_in_background: true`).
- **Ceiling (soft)**: `goalflight_context_audit.py` tracks
  `foreground_over_10s_count` per session; doctor surfaces a WARN
  above a calibrated threshold (e.g., 5 per 100 Bash calls).
- The hook MUST be scope-gated (per `docs/install/context-discipline-hook.md`):
  fires only inside the goal-flight repo or when
  `GOALFLIGHT_HOOKS_FORCE=1`. Without scope gate the hook would break
  unrelated Claude Code sessions.

## Override

Operator can set `GOALFLIGHT_FG_OVERRIDE=1` for an ad-hoc Bash call
that the operator knows is fast (e.g., a one-line probe). Override
emits a WARN to the audit feed.

## Hook implementation status

This protocol defines the data contract + enforcement intent. The
PreToolUse hook script and `settings.json` integration land as a
Wave-A scaffolding follow-up after the canonical scope-gate pattern
(landed in commit `ba27e76`) is verified safe in additional sessions.
