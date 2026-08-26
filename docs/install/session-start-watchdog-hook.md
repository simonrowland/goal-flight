# Install the SessionStart event-wake recovery hook

Goal Flight uses event wakes for normal controller work. A claimed controller prefers one supervised feed; only a controller confirmed to be unsupervised backgrounds an ownership listener, while an unclaimed fixed-set controller backgrounds `goalflight_status.py --wait <ids>`. A live supervisor is restarted and UNKNOWN supervision is resolved before any direct component is armed. These wake promptly for owned worker terminal/escalation events and addressed mail; the canonical operating rule lives in `protocols/dispatch-routing.md` and `commands/execute.md`.

Claude Code also has an hourly, self-suspending in-session cron as crash recovery. Its only job is to recover when the controller or background event wait was lost. If a live event wait already covers the session's in-flight dispatches, the cron reports that fact and does nothing else. The hourly interval bounds recovery of a lost event path to 60 minutes while avoiding a polling work loop.

The cron is not durable: app restart, software update, or reboot clears it. The SessionStart hook is durable configuration. On a fresh Claude Code session in the goal-flight repo, it conservatively detects active or recent Goal Flight work and injects event-wake-first recovery context: arm the background event wait, resume in-skill, then ensure the hourly fallback cron exists. New dispatches arm or retain the wait; they do not re-arm the cron.

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

This hook does not launch the event wait or call `CronCreate`. Hooks are shell commands; background tasks, `CronCreate`, `CronList`, and `CronDelete` belong to the Claude Code controller session. The hook injects `additionalContext` that leads with the background wait and demotes `CronList`/`CronCreate` to ensuring the hourly crash-recovery fallback exists.

Installing the Claude Code Goal Flight plugin loads `hooks/hooks.json`; no user-global `~/.claude/settings.json` edit is required for this hook. For local development without the plugin, copy the same `SessionStart` entry into `.claude/settings.json` and replace `${CLAUDE_PLUGIN_ROOT}` with `${CLAUDE_PROJECT_DIR}`.

Codex, grok, Cursor, and OpenCode orchestrators need equivalent background event-wake and crash-recovery capabilities. Track those as per-host adapter `wake` or `watchdog` capabilities, as sketched in `docs-private/research/2026-05-31-watchdog-injection-plan.md`; do not assume Claude Code cron tools exist outside Claude Code.

## Check

Run:

```bash
scripts/hooks/goalflight-session-start-watchdog.sh --self-test
```

Expected:

```text
PASS: goalflight-session-start-watchdog self-test
```

The self-test proves event-wake-first injection, hourly fallback recovery context, canonical prompt doctrine, active dispatch injection, recent resume-note injection, no-active-run silence, and out-of-scope silence.
