---
description: "Initialize goal-flight state for a project."
---

# init <topic>

Initialize a project for goal-flight with compact, procedural discovery.

Read `protocols/session-preflight.md`, `protocols/tool-readiness.md`,
`protocols/premises.md`, `protocols/state-handoff.md`, and
`protocols/chunk-review.md` (review tooling).

## Steps

1. Confirm host setup/bootstrap happened. If this is a fresh host install, run
   setup from your Goal Flight clone (default `~/.goal-flight`) before project init:

```bash
./setup.sh --agent codex
./setup.sh --apply --yes --agent codex

./setup.sh --cursor
./setup.sh --apply --yes --cursor

# Optional Cursor project-local install:
./setup.sh --apply --yes --cursor-project "$PWD"
```

After source `SKILL.md`, `commands/`, `protocols/`, `templates/`, or `adapters/`
changes, copied host installs need a resync from the source repo with
`./install.sh <host>` or the matching setup path unless the host skill path is a
symlink. Doctor JSON reports `installed_skill_drift`, and text mode prints
`installed_skill_md_hash` WARNs.

For Codex, setup registers the Goal Flight package for the Desktop controller
surface, cleans any duplicate legacy personal Codex skill when plugin
registration succeeds, checks the CLI worker surface, and registers
context-mode MCP when needed. `codex exec` remains the worker path. For Cursor,
setup installs global agent instructions, a personal skill, rules, and Cursor
MCP config for context-mode. Project-local setup writes the wrapper and, when
context-mode is selected, project-specific `.cursor/mcp.json` under the target
repository.

Default add-ons (same tier as setup prompts):

- **context-mode** — large-output offload
- **gstack** — default independent reviewer for both chunk-level pre-commit
  review and milestone review (`/review`, plus `/office-hours`, `/plan-eng-review`,
  `/cso`, `/investigate`). Local fallback prompts live in
  `prompts/gstack-claude-review.md` and `prompts/gstack-codex-challenge.md` when
  gstack is not installed.
- **autoreview** — complementary diff-local pre-commit pass via
  `scripts/autoreview.sh`. Runs in parallel with gstack at chunk level when the
  controller chooses; does not replace gstack as the default review path.
  Install upstream autoreview before init when doctor WARNs — typically the
  Cursor autoreview skill or `AUTOREVIEW_HELPER`.

2. Run doctor:

```bash
python3 <skill-root>/scripts/goalflight_doctor.py --project-root "$PWD" --json
```

   On native Windows, inspect the JSON `wsl` field before any dispatch-shaped
   setup. Goal Flight's full dispatch baseline is WSL, not a native-Win32 port.
   The WSL probe is usable only when **all** are true:

   - `wsl.exe` is present.
   - `wsl -l -q` lists at least one installed distro.
   - A default-distro launch probe succeeds.

   `wsl.exe` present with zero distros is **not** usable. If the operator says a
   distro exists but the probe reports `no_installed_distributions`, inspect
   `wsl.probe.stdout`, `wsl.probe.stderr`, and `wsl.probe.distributions` before
   offering install; the usual traps are UTF-16LE/NUL output from `wsl -l -q`,
   localized no-distro prose, and enterprise-policy guidance text. If
   `wsl.probe.usable` is false and `wsl.probe.declined` is false, ask the
   operator with the controller's host-neutral user-question surface ("Ask User
   Question" on hosts that expose it) before any install command:

   - **Install WSL now** — controller may run `wsl --install`; surface that this
     can require admin elevation, downloads a distro, and can require a reboot
     before dispatch works.
   - **Keep native read/plan only** — write
     `docs-private/windows-wsl-install-declined.json` using
     `goalflight_compat.record_wsl_install_declined(project_root)` (or the same
     schema) so init does not nag every run.

   If `wsl --install` returns nonzero, asks for reboot, is declined, or is
   otherwise pending, treat that as nonfatal and continue init in native
   control-plane mode:
   doctor/status/plan/capacity reads work, dispatch entry points keep refusing
   with the WSL next step, and stale cleanup is degraded to identity-checked
   per-pid cleanup.

   Inside WSL, inspect `wsl_filesystems.warnings`. If project root,
   `GOALFLIGHT_STATE_DIR`, fleet dir, fleet lock dir, or `worktrees/` is under
   `/mnt/<drive>`, warn and move to a WSL-native path before dispatch. DrvFs
   does not provide reliable POSIX `flock` semantics for Goal Flight locks.

3. Ensure the ACP SDK venv exists. On native Windows without usable WSL, skip
   this block; it is POSIX/WSL dispatch setup and the `bin/python` path is
   intentionally not valid for native read/plan mode.

```bash
ACP_VENV="$HOME/.goal-flight/venvs/acp-0.10"
if command -v uv >/dev/null 2>&1; then
  [ -x "$ACP_VENV/bin/python" ] || uv venv "$ACP_VENV"
  uv pip install --python "$ACP_VENV/bin/python" -r <skill-root>/requirements.txt
else
  [ -x "$ACP_VENV/bin/python" ] || python3 -m venv "$ACP_VENV"
  "$ACP_VENV/bin/python" -m pip install -r <skill-root>/requirements.txt
fi
```

4. Run capacity profile:

```bash
python3 <skill-root>/scripts/goalflight_capacity.py profile --json
```

5. Scaffold private project state if missing:

- `docs-private/`
- `docs-private/goal-<topic>-<date>.md` from `templates/goal-statement.md`
- `docs-private/RESUME-NOTES-<YYYY-MM-DD>.md` from `templates/resume-notes.md`
  (canonical naming: ISO 8601 date so lexicographic sort = chronological; no
  topic prefixes — topic context goes inside the file's TL;DR)
- `AGENTS.md` handling (downstream projects often keep AGENTS.md
  per-operator and gitignored on purpose — that's fine; the file is
  still the local skill entry point):
  - **absent**: write from `templates/project-agents.md`. If the path is
    NOT gitignored, `git add -- AGENTS.md`. If gitignored, leave
    untracked — the operator wants it that way.
  - **present, no `## Goal Flight Routing` section**: APPEND the Goal
    Flight Routing block + top-of-file blockquote activation directive
    from the template (idempotent — check for the section header first).
    Don't change the file's git-tracking state.
  - **present, already has the section**: skip (no-op).
  - For projects with multiple operators / public history that want the
    goal-flight routing tracked: maintain `.agent-context/goal-flight.md`
    separately, tracked, and reference it from the (per-operator,
    gitignored) AGENTS.md. Init does not handle that split automatically
    — operator decides.
- `SKILL.md` from `templates/project-skill.md` when project has no root skill

6. Write only compact environment facts into `docs-private/env-caveats.md`:

- doctor summary path/result
- capacity profile
- available worker adapters
- known cooldowns
- commands the project uses for test/lint/build

Do not paste full probe output.

7. Confirm git hygiene:

- `docs-private/` runtime contents ignored (skeleton README and `.gitkeep` placeholders may be tracked in the skill repo; populated project state stays local)
- `AGENTS.md` tracked or intentionally absent
- root `SKILL.md` tracked or intentionally absent
- current branch/head/dirty state recorded in resume notes

8. Optional corpus:

If the repo is large and the user wants reusable dispatch context, run
`/goal-flight build-corpus`. Do not run corpus construction by default during
init.

9. Self-review:

- Are readiness warnings actionable?
- Did init avoid reading large docs/logs into context?
- Are next steps clear from `RESUME-NOTES.md`?

## Output

Print:

- created/updated file paths
- doctor WARN/FAIL count
- capacity operating cap
- next command suggestion
