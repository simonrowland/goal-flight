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
verify drift.

- `claude plugin validate <goal-flight-root>`
- `claude --version`
- Check Codex Desktop:
  - `/Applications/Codex.app`
  - `mdfind 'kMDItemCFBundleIdentifier == "com.openai.codex"'`
- Check `codex --version`.
- If Codex Desktop exists but `codex` CLI is missing, WARN with:
  `Codex Desktop found, but codex CLI missing. Install CLI with npm install -g @openai/codex && codex login. Desktop install implies the user likely already has an OpenAI account; CLI login should use that account.`
- Check context-mode registration with `scripts/register-context-mode-codex.py --check`.
- Check `gstack --version`.
- Check Cursor Desktop, `cursor`, and `cursor-agent`.
  - `/Applications/Cursor.app`
  - `command -v cursor`
  - `command -v cursor-agent`
- Check Grok Build, including headless flags.
  - `--prompt-file`
  - `--permission-mode`
- Check ACP worker adapters: `codex-acp`, `cursor-agent`, `claude-code-cli-acp`, `grok agent stdio`.
- Check project git state.
- Check machine capacity profile.

## Output

Human mode prints an <=80 line checklist.

JSON mode emits `goalflight.doctor.v1`.

WARN means usable with caveat. FAIL means the requested path should not run until
fixed. Missing optional workers should route dispatch elsewhere.
