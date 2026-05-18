# Tool Readiness Protocol

Use procedural checks. Do not re-derive readiness manually unless a script fails.

Primary command:

```bash
python3 <skill-root>/scripts/goalflight_doctor.py --project-root "$PWD"
```

JSON mode:

```bash
python3 <skill-root>/scripts/goalflight_doctor.py --project-root "$PWD" --json
```

Readiness decisions:

- Claude plugin validation failure: block plugin release.
- Codex Desktop present but `codex` CLI missing: suggest `npm install -g @openai/codex && codex login`; Desktop implies the user likely has an OpenAI account.
- Cursor Desktop without `cursor`: suggest Cursor command-palette shell-command install.
- Cursor Desktop without `cursor-agent`: Cursor manual use is possible; ACP worker use is not.
- Grok binary without Grok Build/headless flags: do not route headless work to Grok.
- No ACP adapters: dispatch falls back to Bash-tail watcher.
- context-mode missing on the side that will process large output: warn before long review or log-heavy command.

Capacity decisions come from:

```bash
python3 <skill-root>/scripts/goalflight_capacity.py status --json
```

Use capacity `operating_cap` for scheduling. Treat raw RAM ceiling as a hard
safety bound, not the desired concurrency.
