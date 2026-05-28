# draft-goal-office-hours fixture prompt

You are running a Goal Flight controller behavior test.

Repository: {{PROJECT_ROOT}}

Simulated user request:

"help me build something cool"

The request is too fuzzy for implementation. Route it to the controller's
question-discovery subroutine first.

Reply as the controller with:

- `DISPATCH: <canonical office-hours or ask-questions route>`
- one sentence explaining why implementation waits
- `COMPLETE: true`

Do not edit repository files. Do not start implementation work.
