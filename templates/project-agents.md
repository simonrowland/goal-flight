# Agent Operating Instructions — <PROJECT_NAME>

Private. Read before touching code.

> **Goal Flight is installed in this project.** Check active state FIRST:
> `python3 ${GOALFLIGHT_ROOT:-~/.goal-flight}/scripts/goalflight_session_status.py --text`
> — definitive verdict. If **active**, follow the load order in
> "Goal Flight Routing" below. If **no active session**, do NOT auto-load
> goal-flight's SKILL.md — only load when the user invokes `/goal-flight`.
>
> **Skill-freshness check (post-compaction):** if a system reminder says
> the skill was previously invoked but you can't quote its preamble
> verbatim, re-invoke `/goal-flight` to reload SKILL.md fresh — truncated
> system reminders silently drop load-bearing rules across compactions.
> Then verify you're the designated orchestrator via session-id lookup
> (`goalflight_session_status.py --ensure-session` vs the active queue's
> `current_session.id`).

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
- **activation check** (run before loading the skill):
  `python3 ${GOALFLIGHT_ROOT:-~/.goal-flight}/scripts/goalflight_session_status.py --text`.
  Only proceed with the rest of the load order when the verdict is active.
- load order: this `AGENTS.md` → installed host wrapper (codex/grok/cursor/
  opencode hold a copy; native Claude symlinks) → repository `SKILL.md` →
  only the invoked `commands/*.md` plus referenced `protocols/*.md`.
- keep `docs-private/` ignored; store env caveats, queues, ledgers, review
  outputs, and resume notes there.
- use the project commands above for verification before closing a chunk.
- **post-compaction reload**: run the activation check first; if active,
  read newest `docs-private/RESUME-NOTES-<YYYY-MM-DD>.md` and the active
  goal-queue's frontmatter. Full sequence in `protocols/state-handoff.md`.

## Git workflow

- Commit each logical chunk after focused tests **and at least one independent
  review** (`protocols/chunk-review.md`; default gstack `/review`, with
  `./scripts/autoreview.sh` as a complementary parallel option).
- While other goal-flight workers are in flight, `git commit -m '...' -- <files>`
  with explicit pathspecs — never bare `git commit`. The commit guard
  (`scripts/goalflight_commit_guard.py`) refuses to prevent bundling worker WIP.
- Do not push without explicit permission.
- **Spawned tasks (chips):** a `spawn_task` chip runs in its own git worktree that is
  only auto-removed when left UNCHANGED — so commit your work cleanly (don't strand a
  dirty worktree for someone to hand-clean) and report the branch + commit SHA in your
  return so the initiator can integrate it and drop the worktree.
- For review/analysis, prefer a sub-billed engine (Codex — the autoreview default — or
  grok) over a Claude review flight when conserving Claude usage; sub-billed worker
  capacity is abundant, the Claude budget is not.
