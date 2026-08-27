# Pinned prompt — `status-r2-notfixed`

- source: `inline`
- inline prompt present: yes
- pinned_at: 2026-08-27T14:54:06.319466+00:00

```
ADVERSARIAL NOT-FIXED REVIEW, round 2. Repo: /Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator, branch engine-2026-08-16.

DO NOT run git checkout, git stash or git restore - the working tree holds the unstaged changeset you are reviewing and a checkout would DESTROY it. git diff, git show and plain file reads only.

SCOPE: the uncommitted working-tree diff PLUS the untracked file tests/chemistry/test_provider_status_vocabulary_guard.py, which git diff does NOT show - read it explicitly. Run git status to see it.

BACKGROUND. Round 1 fixed a fail-open in the MAGEMin provider: it kept a private four-token status allowlist and mapped every other adapter status to ok. A review of round 1 returned CHANGES REQUIRED with two P1 and two P2. Round 2, which you are reviewing, addressed all four:
1. simulator/core.py now RE-RAISES IntentResultStatusError instead of letting a generic handler catch it. That handler classified by SUBSTRING MATCH on the exception message and turned an unrecognised engine status into unavailable, which routed to a floor fallback declaring the melt fully liquid above 1200 C and could commit an fe_redox_respeciation ledger transition. Round 1 was cosmetic on that path.
2. engines/alphamelts/parser.py and provider.py: an absent or empty status now means unavailable, not ok.
3. engines/vaporock/provider.py validates the engine-outcome token against the owner vocabulary before wrapping it in the intentional non_authoritative role result.
4. Tests at three levels: mechanism (provider raises), consequence (freeze gate does not authorise), class (AST guard that no provider re-derives the vocabulary).

YOUR NULL HYPOTHESIS IS THAT ROUND 2 IS ALSO INCOMPLETE. Press these three:

A. IS THE EXCEPTION STILL RE-SILENCED SOMEWHERE ELSE? Round 1 died on exactly this. IntentResultStatusError now escapes the provider AND the redox path. Trace every other frame that can see it: the planner, the kernel, the optimizer, the runner, web handlers, any broad except in a caller of a caller. If ANY of them catches it and substitutes a permissive default, round 2 has the same defect one frame further out. THIS IS THE MOST LIKELY WAY ROUND 2 IS WRONG - prioritise it.

B. DID THE CORE.PY CHANGE BREAK A LEGITIMATE PATH? The floor fallback exists for a real reason: a genuinely unavailable liquidus is a measurement failure, and asserting solid without data would be worse physics. That behaviour MUST survive. Check that ProviderUnavailableError and genuine non-convergence still reach the fallback unchanged, and that nothing else that used to be caught now escapes and crashes a run that previously completed. A regression here is worse than the bug being fixed.

C. IS THE ALPHAMELTS ABSENT-STATUS CHANGE SAFE? provider.py previously accepted a statusless sample as ok and read liquid_fraction and liquid_composition_wt_pct off it. Now it refuses. Find whether any real adapter path, cache path, or fixture produces a result without a status, and whether refusing it turns a working run red.

Also check the three test levels for the can-this-test-fail property, and say whether any of them passes against the UNFIXED code. I have already run counterfactuals on the MAGEMin hunks, the core.py re-raise and the VapoRock validation - each goes red when its fix is removed, with the sibling legitimate-path tests staying green. Tell me instead whether the tests pin the right things or leave a gap.

METHOD: verify against code on disk. Every finding needs a concrete path - inputs, frames traversed, wrong result. No style or naming findings. Finding nothing is a real and acceptable result; say which attack you pressed hardest and could not land. Use .venv/bin/python; wrap any pytest in an external wall timeout.

DELIVERABLE: docs-private/reviews/2026-08-26-milestone-engine/status-round2-notfixed.md

Finish your final message with the single line COMPLETE: status-r2-notfixed
```
