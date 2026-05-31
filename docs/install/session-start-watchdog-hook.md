# Install the SessionStart watchdog re-arm hook

Goal Flight uses a Claude Code in-session cron as the controller watchdog. That cron is not durable: app restart, software update, or reboot clears it. The SessionStart hook is durable configuration. On a fresh Claude Code session in the goal-flight repo, it conservatively detects active or recent Goal Flight work and injects a re-arm instruction.

## Files

- Hook: `scripts/hooks/goalflight-session-start-watchdog.sh`
- Canonical watchdog prompt: `templates/goalflight-watchdog-prompt.md`
- Claude Code plugin hook config: `hooks/hooks.json`

The hook only emits context when the session cwd is under this repo and at least one active/recent signal exists:

- a `running` dispatch status under `/tmp/goal-flight-*/dispatch/*.status.json` for this repo
- a `docs-private/RESUME-NOTES-*.md` modified within two days
- `scripts/goalflight_session_status.py --text` reports an active session

Otherwise it prints nothing.

## Claude Code specificity

This hook does not call `CronCreate`. Hooks are shell commands; `CronCreate`, `CronList`, and `CronDelete` are Claude Code tools. The hook only injects `additionalContext` telling the controller to run `CronList` and re-create the cron if absent.

Installing the Claude Code Goal Flight plugin loads `hooks/hooks.json`; no user-global `~/.claude/settings.json` edit is required for this hook. For local development without the plugin, copy the same `SessionStart` entry into `.claude/settings.json` and replace `${CLAUDE_PLUGIN_ROOT}` with `${CLAUDE_PROJECT_DIR}`.

Codex, grok, Cursor, and OpenCode controllers need their own re-arm mechanism. Track that as a per-host adapter `wake` or `watchdog` capability, as sketched in `docs-private/research/2026-05-31-watchdog-injection-plan.md`; do not assume Claude Code cron tools exist outside Claude Code.

## Check

Run:

```bash
scripts/hooks/goalflight-session-start-watchdog.sh --self-test
```

Expected:

```text
PASS: goalflight-session-start-watchdog self-test
```

The self-test proves active dispatch injection, recent resume-note injection, no-active-run silence, and out-of-scope silence.
