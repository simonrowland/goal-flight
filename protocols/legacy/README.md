# Legacy dispatch recipes

Load these **only when the primary path is unavailable**. The ACP transport
(`scripts/goalflight_acp_run.py`) is the default for codex, grok, cursor, and
Claude. Bash-tail and `tail -f` shapes pre-date the ACP adapters; they remain
correct but lose structured turn boundaries, tool-call locations, and clean
stop reasons.

Do not hot-load these documents during `init`, `decompose-plan`,
`ask-questions`, or `execute`. Consult them only when:

- the worker has no ACP adapter on this machine,
- the ACP adapter is known-broken on the worker's version,
- the user explicitly asks for a flat-tail recipe for parity with an older
  script,
- you are reading historical commit messages or review transcripts that refer
  to the bash-tail era.

| File | Use when |
|---|---|
| `bash-tail.md` | spawning codex/grok/claude headlessly with stdout/stderr to a file, watched via marker grep |
| `tail-f.md` | observing a worker process whose lifecycle you do not own (already running, started outside goal-flight) |

If you find yourself loading these for routine work, stop and check whether
the missing adapter is a real environment problem worth fixing instead.
