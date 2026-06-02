# goal-flight

Goal Flight is available as an OpenCode orchestrator workflow when installed from
the cloned repository with `./setup.sh --apply --yes --opencode`.

When a task asks for Goal Flight, long-running orchestration, review flights,
resumable handoff notes, or worker dispatch:

- Load the OpenCode skill from `.opencode/skills/goal-flight/SKILL.md`,
  `~/.config/opencode/skills/goal-flight/SKILL.md`, or
  `~/.agents/skills/goal-flight/SKILL.md`, in that order when present.
- In the target repository, read `AGENTS.md` first when present.
- Then read the repository root `SKILL.md` as the canonical Goal Flight
  workflow.
- Treat repository Markdown plus Git as canonical state.
- Keep queue, ledger, status, review, and handoff artifacts file-backed.
- Treat OpenCode session state, chat, and host config as advisory.
- Treat setup as host registration plus machine bootstrap. Treat init as the
  target-project readiness pass: doctor, capacity, worker readiness, and compact
  caveats.
