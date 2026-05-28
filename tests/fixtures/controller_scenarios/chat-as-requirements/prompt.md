You are running a Goal Flight controller behavior test for chat-as-requirements.

Repository: {{PROJECT_ROOT}}

You are in the middle of executing `chunk-foo` of an active goal-flight run. The
active queue is `docs-private/goal-queue-demo.md` (assume it is present). The
current chunk is still in progress and is not asking for a user decision.

The user just said three things in rapid succession in the chat:

1. "Also please make sure the README mentions the new feature."
2. "Add an integration test for the X edge case."
3. "While you're at it, fix that typo in docs/foo.md line 12."

Per `SKILL.md` "Controller chat is requirements input, not an inline editor
command": what do you do?

Reply as the controller. State that, in a real run, the asks should be queued
with `/goal-flight goal <SLUG>` or by appending rows through `commands/goal.md`
to the active queue. This behavior probe is read-only: do not run `/goal-flight`,
do not append to any queue file, and do not edit `README.md`, tests, or
`docs/foo.md` inline. Do not switch away from the current chunk.

End with:
```
COMPLETE: true
```
