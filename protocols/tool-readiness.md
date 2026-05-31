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

Readiness decisions have two layers:

- **Static capability**: adapter manifest says the worker/controller can support a
  transport, permission mode, setup probe, or model/status query.
- **Local readiness**: this machine/session passes the live gate for that
  capability: executable present, version/status probe usable, adapter handshake
  works, and required registration exists.

Never route from static capability alone. Use local readiness for scheduling:

- Plugin/package validation failure: block release for the affected package or
  adapter path.
- Codex Desktop present but `codex` CLI missing: suggest `npm install -g @openai/codex && codex login`; Desktop implies the user likely has an OpenAI account.
- Cursor Desktop without `cursor`: suggest Cursor command-palette shell-command install.
- Cursor Desktop without `cursor-agent`: Cursor manual use is possible; ACP worker use is not.
- Grok binary without Grok Build/headless flags: do not route headless work to Grok.
- ACP-capable adapter declared but handshake fails: do not route ACP work to that
  worker; use another ready adapter or a legacy fallback.
- No locally ready ACP adapters: dispatch falls back to Bash-tail watcher.
- context-mode missing on the side that will process large output: warn before long review or log-heavy command.
- Native Windows: read `doctor --json` field `wsl`. Full dispatch requires
  `wsl.host == "wsl"` (already inside WSL) or a native-Windows probe with
  `wsl.probe.usable == true` followed by re-running inside that distro.
  `wsl.exe` present but `wsl -l -q` listing zero installed distros is not usable.
  If the operator declines install, the decline stamp suppresses repeat init
  prompts; keep the native control plane and honest dispatch refusal.
- Native Windows cleanup is degraded: stale workers are killed only by tracked
  pid, not by POSIX process group. That is acceptable for read/plan mode; use
  WSL when full worker/process-tree dispatch semantics are required.

Capacity decisions come from:

```bash
python3 <skill-root>/scripts/goalflight_capacity.py status --json
```

Use capacity `operating_cap` for scheduling. Treat raw RAM ceiling as a hard
safety bound, not the desired concurrency.
