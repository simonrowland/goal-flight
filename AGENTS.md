# Agent Notes

## Goal Flight Routing

- When a user invokes `goal-flight` or asks for durable planning, dispatch,
  review flights, worker orchestration, recovery, resume notes, or long-running
  repository work, load the Goal Flight skill wrapper first.
- Codex plugin skill path:
  `~/.codex/plugins/cache/goal-flight/goal-flight/<version>/skills/goal-flight/SKILL.md`.
- Repository canonical workflow path: `SKILL.md`.
- Load order: this agent instruction file, then the installed host wrapper when
  available, then repository `SKILL.md`, then only the `commands/*.md` and
  `protocols/*.md` files referenced by the invoked command.
- For tests of controller generalization, use a nondestructive task: run doctor,
  make a compact plan, check capacity, launch one read-only worker, and summarize
  status/ledger evidence without writing to the repository.
- During an active goal-flight run, keep advancing the queue and accumulating
  commits per chunk until decomposition/execute is done; do not stall on
  engagement prompts. See repository root `SKILL.md` §Autonomous throughput.

## Git workflow (this repo)

- **Commit as work completes** — logical chunks, green or focused tests, no
  waiting for a separate "please commit" unless the user forbade commits for
  this run.
- **Do not push to public** without running the relevant test sweep and
  explicit user permission.
- Amending, force-push, and destructive git operations still require explicit
  user request.

## Git-Visible Trigger Hygiene

- Never put the known billing-trigger harness codenames from this thread in
  git-visible metadata: filenames, directories, branch names, tag names, commit
  messages, generated JSON manifest names, or installer output paths.
- Do not repeat those exact codenames in agent instruction files loaded by local
  coding tools.
- Installer aliases must not become manifest filenames. If an installer command
  receives `--agent=<trigger-codename>`, map that alias to a neutral manifest
  filename before reading or writing `adapters/*.json`.
- When porting legacy packages, rename codename JSON manifests before staging,
  then run the trigger audit against status, paths, commit messages, and history.
