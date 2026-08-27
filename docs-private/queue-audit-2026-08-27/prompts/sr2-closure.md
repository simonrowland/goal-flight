# Pinned prompt — `sr2-closure`

- source: `inline`
- inline prompt present: yes
- pinned_at: 2026-08-27T14:54:06.319466+00:00

```
CLOSURE REVIEW of a round-2 bugfix. Repo: /Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator, branch engine-2026-08-16.

DO NOT run git checkout, git stash or git restore - the working tree holds the unstaged changeset and a checkout would DESTROY it. git diff, git show and plain file reads only.

SCOPE: the uncommitted working-tree diff PLUS the untracked file tests/chemistry/test_provider_status_vocabulary_guard.py, which git diff does NOT show. Run git status and read it explicitly.

YOUR JOB IS CLOSURE, NOT RE-LITIGATION. A review of round 1 returned CHANGES REQUIRED with four items. Round 2 claims to address all four. For EACH, decide CLOSED / PARTIALLY CLOSED / NOT CLOSED and say what remains:

1. P1 - AlphaMELTS turned an absent adapter status into ok, at engines/alphamelts/parser.py and the equilibrium-crystallization path in engines/alphamelts/provider.py. Round 2 makes absent or empty mean unavailable at both. The required change also asked for an authoritative gate-path regression test. Is one present, and does it exercise the AUTHORITATIVE gate path rather than only the projection?

2. P1 - the redox liquidus caller in simulator/core.py caught every Exception from the freeze-gate curve, classified by SUBSTRING MATCH on the message, and returned a floor fallback declaring the melt fully liquid above 1200 C, which can commit an fe_redox_respeciation transition. The required change was to restrict the fallback to expected availability and convergence failures, re-raising contract and programming errors. ROUND 2 RE-RAISES ONLY IntentResultStatusError. ★ IS THAT ENOUGH? Do other contract or programming errors - TypeError, AttributeError, KeyError from a malformed provider result - still reach the fallback and authorise physics? Name them concretely with the path if so, and say whether inverting the handler to catch ONLY the expected types is the correct fix or whether that carries worse regression risk than the hole it closes. I want your judgement on that trade, not just the defect.

3. P2 - VapoRock never routed its engine-outcome token through the validated vocabulary because it always constructs IntentResult(status=non_authoritative). Round 2 validates the token in the diagnostics projection before wrapping. Check this is the ONLY seam where an external VapoRock token enters, and that the early-return path for a missing equilibrium is correctly exempt rather than an accidental bypass.

4. P2 - round-1 tests pinned the mechanism but not the contract or consequence. Round 2 adds a None/empty projection test, a consequence-level test in tests/chemistry/test_evaporation_freeze_gate.py asserting no ledger transition and no fallback record for an unrecognised status, three VapoRock validation tests, and an AST class guard. Is the integration test genuinely at the consequence level? Does the class guard actually catch the historical defect - it claims to test itself against the verbatim pre-fix expression, so verify that self-test is real and not circular.

THEN, INDEPENDENTLY: is there any REMAINING path by which an engine status this system cannot interpret still ends up authorising physics or being recorded as evidence? That is the actual invariant; the four items are one route to it, not the invariant itself.

METHOD: verify against code on disk, not the diff hunk. Every finding needs a concrete failure scenario with inputs and the wrong result. Distinguish structural from behavioural evidence. A clean verdict is acceptable and useful. P0 wrong physics or silent corruption, P1 wrong behaviour, P2 bounded defect, P3 hygiene.

DELIVERABLE: docs-private/reviews/2026-08-26-milestone-engine/status-round2-closure.md. Lead with the per-item table, then the invariant question, then what you did not check.

Finish your final message with the single line COMPLETE: sr2-closure
```
