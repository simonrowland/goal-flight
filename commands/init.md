---
description: "Initialize goal-flight state for a project."
---

# init <topic>

Initialize a project for goal-flight with compact, procedural discovery.

Read `protocols/session-preflight.md`, `protocols/tool-readiness.md`,
`protocols/project-state-layout.md`, `protocols/task-lifecycle.md`,
`protocols/progress-dashboard.md`, `protocols/premises.md`,
`protocols/state-handoff.md`, `protocols/worker-context-package.md`, and
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

For Codex, setup registers the Goal Flight package for the Desktop orchestrator
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
  review and milestone review. When host gstack skills are missing, init offers:
  **minimal subset** (default: `/review`, `/office-hours`, `/plan-eng-review`,
  plus downloaded community skills `grill-me` from `udecode/plate` and
  `thermo-nuclear-code-quality-review` from `cursor/plugins`), **full pack**, or
  **skip**. Full-pack delegation is used only for hosts the upstream gstack setup
  supports; unsupported hosts fall back to the minimal copy path with a teaching
  message. The minimal path exposes only the Goal Flight subset from an existing
  local gstack checkout/cache and downloads the two public community skills only
  after the same consent prompt; download failures warn and degrade gracefully.
  Download sources are pinned HTTPS URLs; the test-only source override
  (`GOALFLIGHT_GSTACK_EXTERNAL_SOURCE_*`) is ignored unless
  `GOALFLIGHT_ALLOW_EXTERNAL_SOURCE_OVERRIDE=1` is also set, with a visible
  ignored-override warning otherwise.
  Local fallback prompts live in
  `prompts/gstack-claude-review.md` and `prompts/gstack-codex-challenge.md` when
  gstack is not installed.
- **autoreview** — complementary diff-local pre-commit pass via
  `scripts/autoreview.sh`. Runs in parallel with gstack at chunk level when the
  orchestrator chooses; does not replace gstack as the default review path.
  Vendored at `autoreview/`; doctor WARNs if `autoreview/scripts/autoreview` or
  `scripts/autoreview.sh` is missing (override helper with `AUTOREVIEW_HELPER`).

2. Run doctor:

```bash
python3 <skill-root>/scripts/goalflight_doctor.py --project-root "$PWD" --json
```

Dispatch examples in this repo assume the direct default is background:
`python3 <skill-root>/scripts/goalflight_dispatch.py --agent codex --prompt-file p.md --cwd .`.
Use `--submit --drain-on-submit` for durable queue launch and `--foreground`
only for synchronous scripts/tests.

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
   operator with the orchestrator's host-neutral user-question surface ("Ask User
   Question" on hosts that expose it) before any install command:

   - **Install WSL now** — orchestrator may run `wsl --install`; surface that this
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

5. Scaffold or migrate private project state if missing. Default is dry-run.
   Apply is create-if-absent only: never overwrite operator files, never
   force-add ignored state, write a per-repo backup before changes, and preserve
   the repository's existing `docs-private/` gitignore policy. Browser-facing
   dashboard files are generated into repo-root `dashboard/`; keep that path
   ignored with an anchored `/dashboard/` rule.

```bash
python3 <skill-root>/scripts/goalflight_setup.py \
  --scaffold-project-state \
  --target-project "$PWD"
```

Inspect the dry-run JSON first. To mutate, rerun with `--apply --yes`.

The scaffolder copies missing store files into `docs-private/`, copies
browser-facing HTML/JS plus `tasks-data.js` into repo-root `dashboard/`, creates
the canonical state directories, and creates
`docs-private/RESUME-NOTES-<YYYY-MM-DD>.md` from `templates/resume-notes.md`
when no canonical resume pin exists. The canonical state contract is
`protocols/project-state-layout.md`; task status is `protocols/task-lifecycle.md`;
HTML view behavior is `protocols/progress-dashboard.md`.
It updates `AGENTS.md` through temp+rename only when needed so the living-state
pin names the newest `docs-private/RESUME-NOTES-*.md`, not a retired handoff
file. It also branches on `git check-ignore docs-private/`: ignored repos stay
untracked; tracked/private repos keep tracking. It reports
`git check-ignore dashboard/` separately; public repos should normally use
`/dashboard/` because sync regenerates these browser assets. Existing
`docs-private/*.html`, `docs-private/gf.js`, or `docs-private/tasks-data.js`
are legacy dashboard locations; init regenerates canonical copies under
`dashboard/` and leaves those legacy files for operator cleanup. In-flight
dispatch ledger records get `task_ids` backfilled only when derivable from
dispatch metadata or the prompt path.

- `docs-private/goal-<topic>-<date>.md` from `templates/goal-statement.md`
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

6. Lane inventory:

Evaluate the triggering-signals table in `protocols/worker-context-package.md`
for each subsystem the plan will touch. Record a per-lane verdict in the state
skeleton: `package needed` or `not needed`. Explicit no-package verdicts are
recorded too.

7. Write only compact environment facts into `docs-private/env-caveats.md`:

- doctor summary path/result
- capacity profile
- available worker adapters
- known cooldowns
- commands the project uses for test/lint/build

Do not paste full probe output.

8. Confirm git hygiene:

- `docs-private/` policy recorded from `git check-ignore docs-private/` — ignored public repos stay private; tracked private repos are allowed
- `dashboard/` policy recorded from `git check-ignore dashboard/` — generated browser views should normally match `/dashboard/`
- `AGENTS.md` tracked or intentionally absent
- root `SKILL.md` tracked or intentionally absent
- current branch/head/dirty state recorded in resume notes

9. Optional corpus:

If the repo is large and the user wants reusable dispatch context, run
`/goal-flight build-corpus`. Do not run corpus construction by default during
init.

10. Self-review:

- Are readiness warnings actionable?
- Did init avoid reading large docs/logs into context?
- Are next steps clear from the newest `docs-private/RESUME-NOTES-*.md`?

## Output

Print:

- created/updated file paths
- doctor WARN/FAIL count
- capacity operating cap
- next command suggestion
