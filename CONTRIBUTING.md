# Contributing to goal-flight

Thanks for helping improve goal-flight — a portable multi-agent orchestrator skill
(Markdown commands + Python scripts) with host adapters for Claude Code, Codex,
Cursor, and OpenCode.

## What belongs here

Good contributions:

- Orchestrator workflow fixes in `SKILL.md`, `commands/*.md`, and `protocols/*.md`
- Procedural runtime improvements in `scripts/goalflight_*.py`
- Host-specific helpers under `scripts/hosts/<host>/` (keep host logic out of the
  portable core when possible)
- Adapter manifest updates in `adapters/*.json` plus matching validation tests
- Install/source files under `configs/<host>/` (these are install sources — they
  are not copied to the GitHub repo root)
- Public documentation under `docs/` (including `docs/architecture.md`)

Out of scope for this repository:

- Private per-project runtime state (`docs-private/` contents on target repos)
- One-off notes from a local orchestrator run
- Changes that only make sense for a single downstream project without a
  portable rationale

## Development setup

```bash
git clone https://github.com/simonrowland/goal-flight.git ~/.goal-flight
cd ~/.goal-flight
```

Create the ACP SDK venv (required for ACP Python tests):

```bash
ACP_VENV="$HOME/.goal-flight/venvs/acp-0.10"
if command -v uv >/dev/null 2>&1; then
  [ -x "$ACP_VENV/bin/python" ] || uv venv "$ACP_VENV"
  uv pip install --python "$ACP_VENV/bin/python" -r requirements.txt
else
  [ -x "$ACP_VENV/bin/python" ] || python3 -m venv "$ACP_VENV"
  "$ACP_VENV/bin/python" -m pip install -r requirements.txt
fi
```

Run doctor against this repo (or a target project):

```bash
python3 scripts/goalflight_doctor.py --project-root "$PWD"
python3 scripts/goalflight_doctor.py --project-root "$PWD" --json   # compact output
```

Optional: exercise host install paths locally:

```bash
./install.sh cursor /path/to/your/project    # dry-run by default via setup.sh
./install.sh opencode /path/to/your/project
./install.sh codex
```

See `README.md`, `docs/architecture.md`, and host notes in
`docs/hosts/cursor.md` and `docs/hosts/opencode.md` for architecture and install
details.

## Running tests

Run the full harness from the repo root:

```bash
./tests/run.sh
```

The harness discovers:

- `tests/bash/test-*.sh` — bash tests (installers, adapters, host helpers, guards)
- `tests/python/test_*.py` — Python tests (ACP client, pool, runner, procedural scripts)

`tests/python/test_acp_*.py` uses `GOALFLIGHT_ACP_PYTHON` when set, otherwise
`~/.goal-flight/venvs/acp-0.10/bin/python`. If that venv is missing, those tests
fail with an install hint.

Run one test file:

```bash
bash tests/bash/test-agent-adapters.sh
python3 tests/python/test_goalflight_procedural.py
```

Exit code from `./tests/run.sh` equals the number of failed test files.

## Code conventions

- Match existing style: small focused scripts, argparse CLIs, compact JSON
  output, short human checklists for the orchestrator to read.
- Keep diffs minimal. Fix the root cause; do not refactor unrelated code in the
  same change.
- Portable core lives in `SKILL.md`, `commands/`, `protocols/`, and
  `scripts/goalflight_*.py`. Put host-specific behavior in
  `scripts/hosts/<host>/` or host install trees under `configs/<host>/`.
- Load-on-demand protocols stay out of the always-loaded surface unless the
  router explicitly references them (see `docs/architecture.md`).
- Markdown commands describe *what* to run; scripts own deterministic facts
  (capacity, ledger, status, doctor probes).

## Adapter changes

Any edit to `adapters/*.json` or `adapters/agent-adapter.schema.json` must pass:

```bash
bash tests/bash/test-agent-adapters.sh
```

That script validates schema coverage, no-leak rules, and gate behavior. Update
manifest counts or fixtures in the test if you add or rename adapters.

Installer CLI aliases must map to neutral manifest filenames on disk — never
write alias names directly into tracked JSON paths.

## docs-private hygiene

`docs-private/` holds per-target-project runtime state: goal statements, queues,
env caveats, resume notes, review jobs, and similar artifacts. In downstream
projects it must stay gitignored.

Do **not** commit private project notes, local probe dumps, or orchestrator
scratch into this skill repository. Public docs (`docs/`, `README.md`) describe
shipped behavior; they do not replace private runtime files.

`docs-private/` is fully untracked in this repo (root `.gitignore`); the init
flow creates the directories it needs at runtime. Do not track a `.gitkeep`
skeleton for it — tracked exceptions are exactly how private content gets
force-added by accident. Never commit anything under `docs-private/`.

## Git-visible trigger hygiene

Some local agent harnesses use codenames that must never appear in git-visible
metadata. Before opening a PR, check:

- Filenames, directories, branch names, tags, and commit messages
- Generated JSON manifest names and installer output paths
- Agent instruction files that local coding tools load

Refer generically to the **trigger audit** when documenting this policy. Run
`bash tests/bash/test-trigger-guard.sh` if you touch install paths, adapter names,
or repo hygiene checks. Map installer `--agent=` aliases to neutral manifest
filenames before reading or writing `adapters/*.json`.

## Pull requests

1. Describe **why** the change is needed, not only what changed.
2. Link a GitHub issue when one exists.
3. List test evidence (`./tests/run.sh` and any focused scripts you ran).
4. Call out adapter, install, or docs-private impact explicitly.
5. Keep commits and branch names descriptive and neutral (see trigger hygiene
   above).

Small, reviewable PRs are easier to land than sweeping refactors.

## Questions

Open a [GitHub issue](https://github.com/simonrowland/goal-flight/issues) for
bugs, design questions, or install problems. Include doctor JSON output and the
failing test log when reporting runtime issues.
