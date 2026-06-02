You are running a Goal Flight orchestrator behavior smoke test in repository:

{{PROJECT_ROOT}}

Follow Goal Flight load order: read AGENTS.md and SKILL.md if you need context.

Your task:

1. Run exactly this command and wait for it to finish:
   python3 scripts/goalflight_doctor.py --project-root {{PROJECT_ROOT}} --json

2. In your reply (which will be captured in a bash-tail log), summarize:
   - the top-level `"ok"` field from the doctor JSON
   - whether `host_goalflight_install.codex` (or overall host install section) looks healthy
   - one warning or caveat if present

3. End with a line exactly: COMPLETE: true

Do not edit repository files. Do not dispatch workers. This is read-only verification.
