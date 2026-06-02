# compaction-reload-in-skill-continuation fixture prompt

<!-- HARNESS_SENTINEL_PLACEHOLDER: {{SENTINEL}} -->

Simulated post-compaction wake: prior chat history is unavailable.

Repository: {{PROJECT_ROOT}}

Active handoff artifacts in this scenario:

- `docs-private/RESUME-NOTES.md`
- `docs-private/goal-queue-controller-behavior.md`

Per `AGENTS.md` Active run + compaction, resume Goal Flight only because an
active handoff exists. Read `AGENTS.md`, reload `SKILL.md` fresh from disk, then
read `commands/resume.md` before deciding the next orchestrator action.

The harness injected a rotating sentinel into the back half of `SKILL.md`, under
Worker Routing. Discover it from `SKILL.md`; it is not present in this prompt.

Simulated active chunk:

- implementation work remains
- it is convergence-heavy, not controller-direct
- once the worker returns clean, chunk-level review must run before any commit

This is a read-only behavior probe. Do not edit repository files, do not launch
a real worker, and do not make a commit. State the in-skill action you would take.

Reply with:

- acknowledgement that the resume notes / compaction handoff were present
- acknowledgement that you reloaded `SKILL.md`
- `SKILL_RELOAD_SENTINEL_QUOTE: <sentinel found in SKILL.md>`
- `DISPATCH: <canonical worker dispatch action>`
- `REVIEW_BEFORE_COMMIT: <canonical chunk review gate>`
- `COMPLETE: true`
