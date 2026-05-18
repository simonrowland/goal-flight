# doctor

Diagnose whether goal-flight can run safely in the current Claude Code session
and project. Read-only. Do not install, register, update, or mutate config.

## When to invoke

- The user explicitly typed `/goal-flight doctor`.
- Before first use after cloning / updating goal-flight.
- When dispatches, milestone reviews, ACP workers, or context-mode routing behave
  unexpectedly.

## Steps

Run compact checks and print a checklist. Use exact paths and versions when
available. Do not dump large config files.

### 1. Skill / plugin package

- Resolve the goal-flight root. Check both:
  - clone-form: `~/.claude/skills/goal-flight`
  - plugin-form: any `goal-flight` plugin under `~/.claude/plugins`
- If both exist and differ, print WARN with both paths.
- If the current repo has `.claude-plugin/plugin.json`, run:
  `claude plugin validate <goal-flight-root>`
  - PASS if exit 0.
  - FAIL if validator reports an error.
  - WARN if `claude` is missing.
- Print `VERSION`, git short SHA, and the pre-flight `Skill-loaded:` fingerprint
  recipe from `SKILL.md` if the behavior-bearing files exist.

### 2. Claude Code runtime

- `claude --version`
- Detect whether `/branch` or `/fork` is the expected self-delegation spelling
  from the version, per `commands/init.md` step 1.
- Print whether `claude plugin validate` is available.

### 3. Codex runtime

- `command -v codex`
- Check Codex Desktop:
  - `/Applications/Codex.app`
  - If absent, `mdfind 'kMDItemCFBundleIdentifier == "com.openai.codex"'`
    if `mdfind` exists.
  - If Codex Desktop exists but `codex` CLI is missing, WARN with:
    `Codex Desktop found, but codex CLI missing. Install CLI with npm install -g @openai/codex && codex login. Desktop install implies the user likely already has an OpenAI account; CLI login should use that account.`
- `codex --version`
- Check `/goal` support:
  - version should be `>= 0.128.0`
  - `codex features list` should show `goals` enabled
- Run `bash <skill-root>/scripts/install-codex-overrides.sh --check <project-root>`
  - PASS if already trusted.
  - WARN if not trusted and print the exact `/goal-flight register-codex` command.
  - WARN if codex missing.

### 4. context-mode

- Check Claude-side context-mode by running:
  `python3 <skill-root>/scripts/register-context-mode-codex.py --check`
- Interpret exit codes exactly as documented in `commands/init.md`:
  - `0`: PASS / nothing to do.
  - `1`: WARN codex missing `[mcp_servers.context-mode]`; print the register command.
  - `2`: WARN Python version too old.
  - other: FAIL with short stderr tail.
- If `npx` exists, print `npx -y context-mode@latest --version` output.

### 5. gstack

- Check:
  - `~/.claude/skills/gstack`
  - `~/.codex/skills/gstack`
  - `<project-root>/.agents/skills/gstack`
- PASS if Claude-side is present and either codex-side or project-level is present.
- WARN if Claude-side missing: milestone Claude review falls back to
  `prompts/gstack-claude-review.md`.
- WARN if codex-side / project-level missing: parallel codex review falls back to
  `prompts/gstack-codex-challenge.md`.

### 6. Cursor / Grok executors

- Check Cursor Desktop:
  - `/Applications/Cursor.app`
  - If absent, `mdfind 'kMDItemCFBundleIdentifier == "com.todesktop.230313mzl4w4u92"'`
    if `mdfind` exists.
  - PASS if Cursor Desktop is installed; WARN if absent.
- Check Cursor CLI:
  - `command -v cursor`
  - If present, run `cursor --version` and print the first line.
  - WARN if Cursor Desktop exists but `cursor` is missing from PATH; print:
    "Install Cursor shell command from Cursor: Command Palette -> Install 'cursor' command."
- Check Cursor ACP adapter:
  - `command -v cursor-agent`
  - If present, run `cursor-agent --version` and print the first line.
  - WARN if missing; Cursor can still be used manually, but goal-flight cannot
    use Cursor as an ACP worker.
- Check Grok Build:
  - Prefer `command -v grok`; otherwise check `~/.grok/bin/grok`.
  - If present, run `grok --version` and print the full first line, including
    build hash when present (for example `grok 0.1.212-alpha.5 (b7b8204a48)`).
  - Run `grok --help` and PASS if the first line contains `Grok Build`.
  - Check headless capability by confirming `grok --help` contains both
    `--prompt-file` and `--permission-mode`.
  - WARN if the binary is present but not executable, lacks headless flags, or
    does not identify as Grok Build.

### 7. ACP worker adapters

- Probe commands without launching long workers:
  - `command -v codex-acp`
  - `command -v cursor-agent` (also reported in the Cursor section)
  - `command -v claude-code-cli-acp`
  - `command -v grok` and `~/.grok/bin/grok`
- Print which adapters are available.
- If none are available, WARN that dispatch falls back to `[bash-tail]`.

### 8. Project state

- `git rev-parse --show-toplevel`
- `git status --short`
- Check `docs-private/` exists.
- Check latest `docs-private/RESUME-NOTES-*.md`.
- Check latest `docs-private/goal-queue-*.md` or legacy
  `docs-private/*-goal-queue-*.md`.
- If no queue exists, INFO: run `/goal-flight init <topic>` then
  `/goal-flight decompose-plan`.

### 9. Output

Print:

```text
goal-flight doctor

[PASS] ...
[WARN] ...
[FAIL] ...
[INFO] ...

Summary: <ready | usable-with-warnings | blocked>
Next: <one exact command or "none">
```

Severity:

- `blocked`: plugin validation fails, goal-flight root missing, required scripts
  missing, or project root cannot be resolved.
- `usable-with-warnings`: optional companions missing, codex not trusted,
  Codex Desktop present but CLI missing, context-mode missing on codex side,
  gstack missing on one side, Cursor CLI or ACP adapter missing, Grok Build
  missing/misidentified, no queue yet.
- `ready`: no FAIL and no WARN.

Never print more than 80 lines. If a command produces long output, summarize the
status and path to the log instead.
