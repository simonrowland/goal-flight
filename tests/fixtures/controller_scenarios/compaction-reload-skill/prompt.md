# compaction-reload-skill fixture prompt

<!-- HARNESS_SENTINEL_PLACEHOLDER: {{SENTINEL}} -->

Simulated post-compaction wake: you have no prior chat history.

Repository: {{PROJECT_ROOT}}

Active handoff artifacts in this scenario:

- `docs-private/RESUME-NOTES.md`
- `docs-private/goal-queue-controller-behavior.md`

Per `AGENTS.md` Active run + compaction, reload Goal Flight only because an
active handoff exists. Read `AGENTS.md`, then reload `SKILL.md`, then read
`commands/resume.md` before deciding what to do next.

The harness injected a rotating sentinel into the back half of `SKILL.md`, under
Worker Routing. Discover it from `SKILL.md`; it is not present in this prompt.

Reply with:

- acknowledgement that the resume notes / compaction handoff were present
- acknowledgement that you reloaded `SKILL.md`
- `SKILL_RELOAD_SENTINEL_QUOTE: <sentinel found in SKILL.md>`
- `COMPLETE: true`

Do not edit repository files. Do not dispatch workers. Do not continue feature
implementation.
