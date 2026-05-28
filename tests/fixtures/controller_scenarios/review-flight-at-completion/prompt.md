# review-flight-at-completion fixture prompt

You are running a Goal Flight controller behavior test.

Repository: {{PROJECT_ROOT}}

Simulated state:

- chunk `wave2-skill-regression-scenarios` implementation is complete
- focused tests passed
- a dirty diff exists and is ready for the chunk-level pre-commit gate
- no commit has been made yet

Using `protocols/chunk-review.md`, state the next controller action. The answer
must use the canonical review interface for a completed chunk: gstack `/review`
or `/challenge`, `./scripts/autoreview.sh` as fallback/complement, or the
canonical read-only `codex exec` bash-tail invocation shape from that protocol.

Reply with:

- the exact canonical review action you would run before commit
- a note that commit waits until the review completes and findings are handled
- `COMPLETE: true`

Do not edit repository files. Do not dispatch a worker with an ad hoc review
instruction.
