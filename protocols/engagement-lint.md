# Engagement-prompt lint protocol

The controller's job during an active goal-flight run is to **advance the
queue**, not to re-solicit permission for actions the plan already
authorized. Engagement prompts ("want me to continue?", "should I
proceed?", "let me know if…") are a regression class: they convert
autonomous progress into user-blocking turns.

Memory reference: `feedback_aggressive_context_protection_for_controller`
+ `feedback_autonomous_throughput_during_overnight`.
Source data: `docs-private/research/2026-05-28-r2b-protocol-lists/findings.md`.

## Verb patterns (regression triggers)

The controller MUST NOT emit user-facing messages that match the
following substring patterns when the current chunk is already
authorized by the plan / queue / running execute loop.

| Pattern | Severity | Notes |
|---|---|---|
| `want me to` | **red** | strongest signal; observed 14× in 3-file sample |
| `are you still there` | **red** | classic engagement bait |
| `if you'd like` | **yellow** | OK only when surfacing a real `USER-NEED` blocker |
| `should I` | **yellow** | OK only when surfacing a real `USER-CONFIRM` decision |
| `let me know if` | **yellow** | OK only when surfacing follow-up channels (e.g., for an investigation that genuinely needs user steer) |
| `do you want` | **yellow** | OK only when offering a real choice with two divergent plan paths |

**Red** = always a violation during an active run with no blocker.
**Yellow** = only valid when paired with a concrete `USER-NEED:` /
`USER-CONFIRM:` marker per `protocols/worker-markers.md`. Bare yellow =
violation.

## When yellow is NOT a violation

- A real blocker exists (permission, auth, destructive op without plan
  default, irreducible product ambiguity).
- The yellow phrase is part of a `USER-NEED:` / `USER-CONFIRM:` block.
- The chunk's scope explicitly closed and the controller is reporting
  COMPLETE (e.g., "let me know if you want X next" as a forward-pointer
  suggestion, not a continuation gate on the current chunk).

## Enforcement

- **Floor (hard)**: a future PostToolUse hook on the controller's
  outgoing message stream blocks send when red pattern appears without
  a paired `USER-NEED:` / `USER-CONFIRM:` marker.
- **Ceiling (soft)**: doctor probe emits a WARN when sampled session
  logs show > 1 red occurrence per 100 controller messages.
- **Runtime audit**: `goalflight_context_audit.py` already aggregates
  similar ratios; add `engagement_red_per_100` as a tracked metric.

Hook implementation is a Wave-A scaffolding follow-up; this protocol
is the data contract.

## Override

Operator can set `GOALFLIGHT_ENGAGEMENT_OVERRIDE=1` for a specific
ad-hoc session (e.g., genuine product-discovery turn). Override always
emits a WARN to the audit feed.
