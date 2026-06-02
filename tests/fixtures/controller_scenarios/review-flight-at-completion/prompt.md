# review-flight-at-completion fixture prompt

You are running a Goal Flight orchestrator behavior test.

Repository: {{PROJECT_ROOT}}

Simulated state:

- chunk `wave2-skill-regression-scenarios` implementation is complete
- focused tests passed
- a dirty diff exists and is ready for the chunk-level pre-commit gate
- no commit has been made yet

Using `protocols/chunk-review.md`, run the chunk-level review gate before any
commit. In this harness, use the repository autoreview helper as the fallback /
complement. Run it in local dry-run mode with web search disabled so the scenario
exercises a canonical review entrypoint without model dispatch. Do not substitute
a private review instruction.

Reply with:

- the exact canonical review action you ran before commit
- the observed dry-run output from the review helper
- a note that commit waits until the review completes and findings are handled
- `COMPLETE: true`

Do not edit repository files. Use only the canonical review entrypoints above.
