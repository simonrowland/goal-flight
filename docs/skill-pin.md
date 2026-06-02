# Skill pin: a stable install for sibling projects

If you run goal-flight as an orchestrator in **several projects** on one machine but
also **develop goal-flight itself**, the dev tree's uncommitted work-in-progress
leaks into every other project — because the native Claude host resolves the
skill from a single user-level symlink, `~/.claude/skills/goal-flight`, and
whatever that points at is what *every* project gets.

The skill pin fixes this: point the global symlink at a **stable, reviewed
snapshot** by default, and flip the **one** project that develops goal-flight to
the live tree only while you are actively dogfooding it.

## Why a single global lever (not per-project overrides)

Claude Code's documented same-name skill precedence is **personal > project**, so
a project-local `.claude/skills/goal-flight` does **not** reliably override the
user-level one. (Community docs claim the opposite; the official docs and the
search consensus say personal wins, and it is version-dependent.) Do not rely on
a project-level override to differentiate versions — treat the single global
symlink as the one reliable lever.

Consequence: at any moment the global symlink points at exactly one thing for
**all** projects. "Siblings stable, dev live" is therefore achieved in *time*
(flip to live while developing, flip back when done), not in *space*.

## The layout

```
~/.goal-flight/
  skill/      <- git worktree of the dev repo, DETACHED at a stable tag = the PIN
  fleet/      <- (unrelated) fleet bootstrap
  venvs/      <- (unrelated) managed venvs
  .skill-link.json   <- remembers link / pin / live / previous for the toggle

~/.claude/skills/goal-flight -> ~/.goal-flight/skill   (default: the PIN)
```

The pin is a **detached `git worktree` at a tag**, so its content does not move
as dev `HEAD` advances, and it shares the dev repo's object store (cheap — no
second clone). Coupling caveat: the worktree depends on the dev repo's `.git`
existing; if you move or delete the dev repo, re-create the worktree.

## One-time setup

```bash
DEV=/path/to/goal-flight                       # your dev clone
git -C "$DEV" tag -a stable-YYYY-MM-DD <commit> -m "stable pin snapshot"
git -C "$DEV" worktree add --detach ~/.goal-flight/skill stable-YYYY-MM-DD

# Point the global symlink at the pin and remember the live tree:
python3 "$DEV/scripts/goalflight_skill_link.py" --pin --live-path "$DEV"
```

Pick a commit that actually has the features you depend on — an old release tag
may predate the current dispatch architecture. `--pin` refuses any target that
is not a skill (no `SKILL.md`), and refuses to clobber a real (non-symlink)
install unless you pass `--force` (which moves it aside, never deletes it).

## Daily use

```bash
# In the dev repo, to dogfood the live tree this session:
python3 scripts/goalflight_skill_link.py --live

# When done developing, flip back to the stable pin:
python3 scripts/goalflight_skill_link.py --pin      # or: --restore (last target)

# Check what the symlink currently points at, from anywhere:
python3 scripts/goalflight_skill_link.py --status    # add --json for scripts
```

States reported by `--status`: `PIN`, `LIVE`, `OTHER` (some third target),
`REALPATH` (a real dir is installed, not a symlink), `MISSING` (no link).

The toggle runs as a plain file in the dev tree, so it works regardless of what
the skill symlink currently resolves to. It is invoked **by path**, not via the
loaded skill.

## Bumping the pin

When a newer stable snapshot is ready (reviewed, tests green, pushed to origin):

```bash
git -C /path/to/goal-flight tag -a stable-YYYY-MM-DD <newer-commit> -m "..."
git -C ~/.goal-flight/skill fetch --tags
git -C ~/.goal-flight/skill checkout --detach stable-YYYY-MM-DD
```

Siblings pick up the new pin automatically (the symlink already points at the
worktree); nothing else to do.

## Caveats

- **The flip is machine-wide while flipped.** While the global symlink is on
  `--live`, *any* project session that starts gets the live tree. Flip back to
  `--pin` when you finish a dev session. (Most dev — editing files, running
  tests — does not require the skill to be live-loaded; only testing *new
  orchestrator behavior* does.)
- **POSIX/WSL only.** This manages a symlink; native Windows skill dispatch is
  read/plan-only (see `docs/hosts/windows.md`).
- **Do not edit the pin worktree.** It is a detached snapshot; make changes in
  the dev tree, review, commit, then bump the pin.
