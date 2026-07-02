Run `python3 scripts/goalflight_session_status.py --text` first. If active, run
`python3 goalflight_task.py next` and continue the task named by its `CONTINUE:`
directive. Do not wait for a re-prompt when the top task is ordinary worker work.

Then read the newest docs-private/RESUME-NOTES-*.md for environment, ideas,
facts, carriers, and "Do not re-litigate" context. Re-ground in AGENTS.md / Key
pins. If blocked, record the decision in docs-private/questions-for-user.md
(list the task ids it blocks) and move on.

This prompt names no specific tasks on purpose. The living task state is the
store (`status`, `list`, `next`); RESUME-NOTES carries non-store context.
Find newest notes: `ls -1 docs-private/RESUME-NOTES-*.md | sort | tail -1`.
