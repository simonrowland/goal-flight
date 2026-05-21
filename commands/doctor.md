---
description: "Run procedural readiness checks for goal-flight."
---

# doctor

Diagnose whether goal-flight can run safely in the current session. Read
`protocols/tool-readiness.md`, then run the procedural doctor.

## Command

```bash
python3 <skill-root>/scripts/goalflight_doctor.py --project-root "$PWD"
```

For machine-readable output:

```bash
python3 <skill-root>/scripts/goalflight_doctor.py --project-root "$PWD" --json
```

## Required Checks

The script owns the checks; this file names the contract so tests and humans can
verify drift. In the current implementation, doctor reports static adapter
manifest validity plus local availability checks: PATH/binary presence, selected
CLI versions, context-mode registration, gstack, project state, capacity, model
currency probes, and rate-pressure signals.

It does **not** yet prove full worker/controller routeability. The bounded ACP
handshake plus `validate_adapter_gate` routeability gate is forthcoming in Phase
3 chunk 11 and is **not enforced by `goalflight_doctor.py` yet**. Until then,
ACP rows in doctor output mean "binary/entrypoint present on this machine", not
"structured dispatch has passed a live ACP handshake".

- Validate goal-flight packaging and adapter manifests. Host-native validators
  are adapter-owned compatibility probes, not the core readiness source.
- Check Codex Desktop:
  - `/Applications/Codex.app`
  - `mdfind 'kMDItemCFBundleIdentifier == "com.openai.codex"'`
- Check `codex --version`.
- If Codex Desktop exists but `codex` CLI is missing, WARN with:
  `Codex Desktop found, but codex CLI missing. Install CLI with npm install -g @openai/codex && codex login. Desktop install implies the user likely already has an OpenAI account; CLI login should use that account.`
- Check context-mode registration with `scripts/register-context-mode-codex.py --check`.
- Check `gstack --version`.
- Check first-class local worker/controller candidates:
  - Codex: Desktop/CLI present, context-mode registered when large-output work
    will run, ACP adapter binary present when the Codex ACP path is considered.
  - Cursor: Desktop, `cursor`, and `cursor-agent` present when Cursor is routed
    as controller or worker.
    - `/Applications/Cursor.app`
    - `command -v cursor`
    - `command -v cursor-agent`
  - Grok: Grok Build present, headless flags available.
    - `--prompt-file`
    - `--permission-mode`
  - Claude compatibility path: CLI/plugin checks pass before Claude-specific
    compatibility examples are used.
- Check ACP worker adapters for presence/PATH only: `codex-acp`,
  `cursor-agent`, `claude-code-cli-acp`, `grok agent stdio`.
- Check project git state.
- Check machine capacity profile.
- **Cursor model currency** (`cursor_models_probe`): runs `cursor-agent --list-models`,
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

## Output

Human mode prints an <=80 line checklist.

JSON mode emits `goalflight.doctor.v1`.

WARN means usable with caveat. FAIL means the requested path should not run until
fixed. Missing optional workers should route dispatch elsewhere.
