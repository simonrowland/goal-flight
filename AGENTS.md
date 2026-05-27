# Agent Notes

## Companion tools (defined for non-Claude controllers loading this file)

- **gstack** — Garry Tan's skill pack (`/review`, `/challenge`, `/office-hours`,
  `/plan-eng-review`, `/cso`, `/investigate`, etc.). Installs at
  `~/.gstack/repos/gstack/.agents/skills/` and is registered per-host
  (Claude Code: `~/.claude/skills/`; Codex: `~/.codex/skills/`; Cursor:
  `~/.cursor/skills/`). Goal Flight invokes `gstack /review` as the canonical
  chunk-level pre-commit reviewer and `gstack /challenge` for adversarial
  framing. When gstack is absent, fall back to the bundled prompt skeletons at
  `prompts/gstack-claude-review.md` and `prompts/gstack-codex-challenge.md` —
  do **not** hand-roll a custom review prompt.
- **context-mode** — MCP plugin that offloads large command outputs
  (diffs, integration test runs, codex tail files, large greps) to an FTS5
  sandbox queried by pattern. Lets the controller analyze big artifacts
  without consuming its own context window. Installs per-host (Claude Code:
  `~/.claude/plugins/cache/context-mode/...`; Codex: registered via
  `scripts/register-context-mode-codex.py`).

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
- **Active run + compaction:** if goal-flight was already in play (user invoked
  it, open queue/ledger, or `docs-private/RESUME-NOTES*.md`), reload the skill
  (load order above → `commands/resume.md`). Not always-on. Details:
  `protocols/state-handoff.md`.

## Git workflow (this repo)

- **Commit as work completes** — one logical chunk at a time after focused tests
  **and at least one independent review** (`protocols/chunk-review.md`; default
  gstack `/review`, with `./scripts/autoreview.sh` as a complementary parallel
  option). Executor self-review alone is not enough.
- Do not wait for a separate "please commit" unless the user forbade commits.
- **Do not push to public** without the relevant test sweep and explicit user
  permission.
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
