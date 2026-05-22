# goal-flight

Goal Flight is available as a Cursor controller workflow when installed from
the cloned repository with `./setup.sh --apply --yes --agent cursor`.

When a task asks for Goal Flight, long-running orchestration, review flights,
resumable handoff notes, or worker dispatch:

- Load the personal Cursor skill at `~/.cursor/skills/goal-flight/SKILL.md`.
- In the target repository, read `AGENTS.md` first when present.
- Then read the repository root `SKILL.md` as the canonical Goal Flight
  workflow.
- Treat repository Markdown plus Git as canonical state.
- Keep queue, ledger, status, review, and handoff artifacts file-backed.
- Treat Cursor chat, rules, and memories as advisory.
- Treat setup as host registration plus machine bootstrap. Treat init as the
  target-project readiness pass: doctor, capacity, worker readiness, and compact
  caveats.
