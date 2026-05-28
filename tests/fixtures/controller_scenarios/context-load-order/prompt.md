# context-load-order fixture prompt

You are running a Goal Flight controller behavior test.

Repository: {{PROJECT_ROOT}}

Question:

What's the canonical review path for this chunk?

Follow the documented Goal Flight context load order before answering. Reply as
the controller with:

- `LOAD_ORDER: <files read in order>`
- the canonical review path for a commit-worthy chunk
- `COMPLETE: true`

Do not edit repository files. Do not dispatch workers.
