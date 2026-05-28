# vague-goal-premise-backlog fixture prompt

You are running a Goal Flight controller behavior test.

Repository: {{PROJECT_ROOT}}

Simulated user request:

"I think we should refactor the auth flow but not sure if it's worth it."

The premise is unclear and should be recorded for premise review rather than
blocking the user for clarification.

Reply as the controller with:

- `BACKLOG: <docs-private/premises-*.md, commands/premises.md, or office-hours backlog row>`
- one compact row describing the premise and uncertainty
- `COMPLETE: true`

Do not edit repository files.
