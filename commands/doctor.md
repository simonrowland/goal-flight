---
description: "Run procedural readiness checks for goal-flight."
---

# doctor

Diagnose whether goal-flight can run safely in the current session. Read
`protocols/tool-readiness.md`, `protocols/project-state-layout.md`,
`protocols/task-lifecycle.md`, and `protocols/progress-dashboard.md`, then run
the procedural doctor.

## Command

```bash
python3 <skill-root>/scripts/goalflight_doctor.py --project-root "$PWD"
```

For machine-readable output:

```bash
python3 <skill-root>/scripts/goalflight_doctor.py --project-root "$PWD" --json
```

For a live worker write-file probe:

```bash
python3 <skill-root>/scripts/goalflight_doctor.py --project-root "$PWD" --worker-write-probe --write-probe-agent grok-code
```

## Required Checks

The script owns the checks; this file names the contract so tests and humans can
verify drift. Doctor separates host-global install readiness from project-local
Goal Flight readiness.

Host-global checks cover PATH/binary presence, selected CLI versions,
host-wrapper installation, context-mode registration, gstack, autoreview,
capacity, model currency probes, and rate-pressure signals.

Project-local checks cover `docs-private/env-caveats.md`, the canonical
`docs-private/` state tree, repository `SKILL.md`, `AGENTS.md` Goal Flight
routing, the `AGENTS.md` newest-`RESUME-NOTES` living-state pin, project
verification commands, resume notes, missing state files, managed-view
schema/template skew, and the `tasks.jsonl` <-> `tasks-data.js` mirror.
Package plugin validation applies only when `--project-root` is the Goal Flight
package repository; for normal target projects it is skipped as INFO.

- Validate goal-flight packaging and adapter manifests. Host-native validators
  are adapter-owned compatibility probes, not the core readiness source.
- Check Codex Desktop:
  - `/Applications/Codex.app`
  - `mdfind 'kMDItemCFBundleIdentifier == "com.openai.codex"'`
- Check `codex --version`.
- If Codex Desktop exists but `codex` CLI is missing, WARN with:
  `Codex Desktop found, but codex CLI missing. Install CLI with npm install -g @openai/codex && codex login. Desktop install implies the user likely already has an OpenAI account; CLI login should use that account.`
- Check Codex context-mode registration with `scripts/register-context-mode-codex.py --check`.
- Check Cursor context-mode registration with
  `scripts/register-context-mode-cursor.py --scope global --check` and
  `scripts/register-context-mode-cursor.py --scope project --project-root "$PWD" --check`.
- Check `gstack --version`.
- Check autoreview: `scripts/autoreview.sh` executable and vendored helper at
  `autoreview/scripts/autoreview` (`AUTOREVIEW_HELPER` overrides).
- Check first-class local worker/controller candidates:
  - Codex: Desktop/CLI present, context-mode registered when large-output work
    will run, ACP adapter binary present when the Codex ACP path is considered.
  - Cursor: Desktop, `cursor`, and `cursor-agent` present when Cursor is routed
    as orchestrator or worker; global or project `mcp.json` has context-mode when
    large-output work will run.
    - `/Applications/Cursor.app`
    - `command -v cursor`
    - `command -v cursor-agent`
    - `~/.cursor/mcp.json`
    - `.cursor/mcp.json`
  - Grok: Grok Build present, headless flags available. File-writing routes
    require the live write-file e2e probe to pass in the current environment;
    otherwise use Grok for read-only analysis/research only.
    - `--prompt-file`
    - `--permission-mode`
    - `--os-sandbox`
  - Claude compatibility path: CLI/plugin checks pass before Claude-specific
    compatibility examples are used. NOTE: presence/PATH only — a green doctor
    does NOT confirm the **remote** claude-acp credential. Headless subscription
    seat readiness is verified per node by the fleet auth probe
    (`claude auth status --json`), not by host-global doctor; pty headroom is
    covered by `pty_shim_health` (orphan reaping), not a pre-dispatch free-pty gate.
- Check ACP worker adapters for presence/PATH only: `codex-acp`,
  `cursor-agent`, `claude-code-cli-acp`, `grok agent stdio`.
- Check `claude_acp_stopgap`; when the stopgap is required but absent, use
  `scripts/install_claude_acp_patch.sh` and re-run doctor.
- Check project git state.
- Check project Goal Flight readiness:
  - `docs-private/env-caveats.md`
  - canonical `docs-private/` files/dirs from `templates/state-skeleton/` and
    `protocols/project-state-layout.md`
  - root `SKILL.md`
  - `AGENTS.md` Goal Flight routing with skill-root/load-order guidance
  - `AGENTS.md` pins the newest `docs-private/RESUME-NOTES-*.md` living state
  - project test/lint/build commands
  - optional `docs-private/RESUME-NOTES*.md`
  - managed static view assets match the templates from
    `templates/state-skeleton/`; schema/template skew is a layout warning
  - `docs-private/tasks.jsonl` and `docs-private/tasks-data.js` mirror
    validation when either task-store file exists
- Check machine capacity profile.
- **Cursor model currency** (`cursor_models_probe`): runs `cursor-agent models`,
  identifies the leading internal `composer-X.Y` (non-`-fast`), compares against
  `~/.cursor/cli-config.json` `modelId`. Flags `user_behind` when the user is on an
  older internal model or on a paid-passthrough model. Surfaces as `[OK]` or `[WARN]`
  with the cli-config edit recommendation.
- **Worker CLI currency** (`worker_currency_probe`): proxy for model currency for
  workers without a native list-models API. `grok update --check --json` for grok;
  `npm view <pkg> version` for `@openai/codex` / `@anthropic-ai/claude-code` /
  `claude-code-cli-acp`. Behind workers get `[WARN]`; verified-current workers `[OK]`;
  probe-failed (registry unreachable) workers `[INFO]` as a separate line.
- **Rate-pressure summary** (`_rate_pressure_summary` → `goalflight_rate_pressure`):
  scans the dispatch ledger for provider-level rate-limit signatures over the last
  10 minutes. `[WARN]` per pressured provider with recommended caps and fallback
  providers; `[OK]` when all providers clear.
- **PTY shim health** (`pty_shim_health`): orphaned claude-acp shims and pty cap
  pressure. Remediation: `bin/gf-reap-shims --exec` (or
  `python3 scripts/goalflight_reap_shims.py --exec`).

## Output

Human mode prints an <=80 line checklist.

JSON mode emits `goalflight.doctor.v1`.

WARN means usable with caveat. Missing optional workers should route dispatch
elsewhere. The CLI exits nonzero for package validation failures; project
readiness and host-install caveats are structured WARN/INFO fields, so
automation should inspect JSON fields such as `project_goalflight_readiness.ok`
and `host_goalflight_install.<host>.ok`.
