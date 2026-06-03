# Session Pre-Flight Protocol

Run before non-trivial goal-flight commands. Keep output compact.

1. Run procedural status first:

```bash
python3 <skill-root>/scripts/goalflight_status.py
```

2. Run doctor when:

- first command in a session
- install/tooling changed
- dispatch/review/capacity behavior looks wrong

```bash
python3 <skill-root>/scripts/goalflight_doctor.py --project-root "$PWD" --json
```

3. Fingerprint behavior-bearing files. Include:

- `SKILL.md`
- `commands/*.md`
- `protocols/*.md`
- `prompts/*.md`
- `templates/*.tpl` and `templates/*.md`
- `scripts/goalflight_*.py`
- `scripts/acp_*.py`

The fingerprint is a drift signal, not a security boundary.

4. Surface only actionable drift:

- multiple goal-flight installs
- loaded fingerprint mismatch vs in-flight queue/resume notes
- missing context-mode on a side that will process large output
- active capacity cooldown or surplus worker-like processes

5. Do not inspect raw logs. If a worker is running, read status JSON or run:

```bash
python3 <skill-root>/scripts/goalflight_status.py
```
