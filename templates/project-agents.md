# Agent Operating Instructions — <PROJECT_NAME>

Private. Read before touching code.

## What this project is

<short description>

## Hard invariants — never break

- <invariant>

## File map

- `<path>` — <purpose>

## Commands

- test: `<command>`
- lint: `<command>`
- build: `<command>`

## Goal Flight Routing

- skill-root: `${GOALFLIGHT_ROOT:-~/.goal-flight}`
- load order: read this `AGENTS.md`, then repository `SKILL.md`, then the
  invoked Goal Flight `commands/*.md` file from skill-root.
- keep `docs-private/` ignored; store env caveats, queues, ledgers, review
  outputs, and resume notes there.
- use the project commands above for verification before closing a chunk.
- active run + compaction: reload skill + `commands/resume.md` (skill-root);
  see `protocols/state-handoff.md`. Not always-on.

## Git workflow

- Commit each logical chunk after focused tests **and at least one independent
  review** (`protocols/chunk-review.md`; default gstack `/review`, with
  `./scripts/autoreview.sh` as a complementary parallel option).
- Do not push without explicit permission.
