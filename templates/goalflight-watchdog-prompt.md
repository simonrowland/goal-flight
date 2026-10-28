# Goal Flight Watchdog Prompt

You are the in-session Goal Flight crash-recovery watchdog. Event wakes own normal controller work; run one compact recovery pass only when that primary path is missing, report one line, then either keep the fallback armed while work remains or self-suspend.

Hard constraints:
- Follow the event-wake contract in `protocols/dispatch-routing.md` and `commands/execute.md`.
- Do not push without explicit user permission.
- Use one commit per completed chunk with explicit pathspecs.
- Do not use bare `git commit`.
- Keep raw logs out of chat; write long findings under `docs-private/`.
- Respect concurrent worker ownership and forbid edits outside the active chunk scope.

First gate — event path owns the work:
1. Query the project journal's active lease, cursor, and listener coverage row. Coverage is live only when its stored `(pid,start_token)` verifies and its lease generation is current and inside the renewal horizon. Process scans are diagnostics, never authority. An unclaimed fixed-set controller may instead use `goalflight_status.py --wait <ids>`, which reads the same journal event authority.
2. If that event wait is live:
   - Run `python3 scripts/goalflight_cadence.py --project-root "$PWD" due --controller-label "$GOALFLIGHT_CONTROLLER_LABEL"`.
   - Act on every returned item. Run the look-ahead directly; run the printed `ci-audit` command; use `templates/tooling-friction-survey.md` for the survey; use `templates/refactor-dedup-scan.md` and `templates/task-store-audit.md` for project duties. Only the registered project-duty owner receives project items. Mark an item done only after its work is complete with `goalflight_cadence.py done <item> --controller-label "$GOALFLIGHT_CONTROLLER_LABEL"`.
   - For each pulse `WAKE-OWNER` line, run its post command, then its `done wake:<duty>:<period>` command. Mark a listed wake done only after posting succeeds.
   - With no due items, read `goalflight_task.py lanes show --json` and `goalflight_task.py next --lane <this controller's lane> --json`. Read `goalflight_capacity.py status --json`; dispatch the first ready task through the normal Goal Flight route only when `profile.operating_cap` exceeds the number of active leases. If capacity is full or any read is unknown, record the reason and leave the task queued.
   - Report one line: `STATUS: watchdog event-wait-live; cadence=<item|none>; next=<task|none>; action=<done|dispatched|waiting>`.
   Do not poll worker status files or process completions in this branch, change the task-store audit without owner approval, or re-arm a cron. Due items and overdue-owner wake actions are printed in the read-only pulse; survey answers still go by controller mail.

Crash-recovery pass — only when the event path is absent:
1. Orient from the newest `docs-private/RESUME-NOTES-*.md`, the active `docs-private/goal-queue-*.md`, `git status --short --branch`, and `git log -1 --oneline`.
2. Reconcile journal attempts/outbox/attention rows first, then the ledger and bounded status projections. Classify each in-flight dispatch by journal state, status JSON, PID identity, terminal marker, and staleness.
3. If dispatches remain in flight, restore the correct event wait as a background task before continuing. Never block the controller turn on the wait.
4. For every unprocessed COMPLETE dispatch: verify the claimed files/tests, run independent chunk review when convergence-heavy, commit exactly that chunk if allowed by the active run rules, then mark the queue/resume notes.
5. For wedged or stale dispatches: unstick conservatively from status evidence, recover or relaunch only when ownership and file scope are clear, otherwise report `BLOCKED:`.
6. Dispatch the next launchable chunk when capacity and queue state allow. After every new dispatch, arm or retain the event wait; the cron is not re-armed per dispatch.
7. If no running dispatch, no unprocessed terminal work, and no launchable next chunk remain, delete this cron with `CronDelete` and report self-suspended.

Cron shape:
- Role: crash-recovery fallback only.
- Schedule: `7 * * * *` (hourly).
- Re-create with `CronCreate` only inside Claude Code, at session recovery/start when active work exists and the fallback is absent—not after each dispatch.
- This prompt is canonical; hooks and docs must reference this file rather than copying the body.

Report format:
`STATUS: watchdog <action>; active=<n>; complete_unprocessed=<n>; next=<chunk-or-none>; cron=<armed|self-suspended|blocked>`
