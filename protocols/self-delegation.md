# Self-Delegation Protocol

Load only when the user explicitly asks for `/fork`, `/branch`, or session
self-delegation, or when a chunk truly needs inherited conversation context.

Default dispatch should use subagents, ACP, Codex CLI, Grok, or Bash-tail
workers. Forking is heavier because it inherits the orchestrator conversation.

Procedure:

1. Orchestrator writes a fork contract before forking.
2. Forked session self-detects against the contract.
3. Fork emits `FORK-COMPLETE` or `FORK-BLOCKED`.
4. Orchestrator monitors compact status, not raw JSONL unless recovery requires it.

Use `scripts/self-fork-detect.sh` for detection. Keep fork protocol out of the
always-loaded kernel.
