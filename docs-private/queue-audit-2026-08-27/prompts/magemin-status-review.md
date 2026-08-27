# Pinned prompt — `magemin-status-review`

- source: `inline`
- inline prompt present: yes
- pinned_at: 2026-08-27T14:54:06.319466+00:00

```
REVIEW AN UNCOMMITTED BUGFIX. Repo: /Users/simonrowland/Library/CloudStorage/Dropbox/Starship Mission Design/Regolith Processing/regolith-pyrolysis-simulator, branch engine-2026-08-16.

ORIENTATION FIRST (both gitignored - read from the working tree, not git): AGENTS.md and CLAUDE.md at the repo root.

SCOPE: the uncommitted working-tree diff only. Run git diff to see it. It touches engines/magemin/provider.py, simulator/chemistry/kernel/__init__.py and tests/chemistry/test_magemin_shadow.py. Do NOT run git checkout, git stash or git restore - the working tree holds the unstaged fix you are reviewing and a checkout would destroy it. git diff, git show and plain file reads only.

WHAT THE FIX CLAIMS TO DO. A milestone audit found that engines/magemin/provider.py kept a private allowlist of four status tokens and mapped every other adapter status to ok, which turned an unrecognised engine answer into an affirmative one. That matters because MAGEMinShadowProvider is the registered fallback for the authoritative GATE_LIQUID_FRACTION intent, and the freeze-gate consumer in simulator/evaporation.py prefers IntentResult.status over the diagnostic backend status when deciding whether to accept solidus and liquidus bounds - so a failed MAGEMin result carrying stale finite bounds could set the liquid-fraction multiplier applied to authoritative evaporation rates. The fix passes the adapter status straight to IntentResult, whose __post_init__ validates against INTENT_RESULT_STATUSES and raises IntentResultStatusError otherwise. A second hunk changes an absent or empty status from defaulting to ok to defaulting to unavailable.

YOUR NULL HYPOTHESIS IS THAT THIS FIX IS INCOMPLETE OR WRONG. Argue that and see whether the code refutes you. Specifically:

1. IS IT FIXED AT THE CHOKEPOINT OR ONLY HERE? Find every other place in the tree that maps, coerces, defaults or filters a backend or engine status before it reaches IntentResult. AlphaMELTS was converted earlier; MAGEMin is this fix. If a third provider or an adapter layer still rewrites statuses, the class is not closed and that is the finding.
2. WHAT BREAKS THAT SHOULD NOT? Passing the token through converts a silent downgrade into a raised exception. Work out concretely which callers can now see IntentResultStatusError escape, whether any of them catch it too broadly and re-silence it, and whether a shadow-only dispatch raising can take down a run that previously completed. A provider that is shadow in one registration and authoritative-fallback in another is exactly where this could go wrong.
3. CAN THE NEW TESTS FAIL? For each of the three added tests, work out what would have to break for it to go red, and whether the specific defect is inside that set. I already ran a counterfactual - reverting both hunks turns the two pins red and leaves the third green - so tell me instead whether the tests pin the RIGHT thing, or whether they pin the mechanism while missing the consequence. The consequence is the freeze-gate accepting bounds it should refuse; there is no test at that level and I want your judgement on whether one is needed.
4. IS THE SECOND HUNK JUSTIFIED OR IS IT SCOPE CREEP? Changing the absent-status default from ok to unavailable is a separate behaviour change bundled into the same fix. Argue both sides and say whether it belongs here.
5. THE KERNEL PACKAGE EXPORT. INTENT_RESULT_STATUSES and IntentResultStatusError were added to simulator/chemistry/kernel/__init__.py exports. Check that this does not create a cycle and does not shadow an existing name.

METHOD: verify against the code on disk, not the diff hunk in isolation. Every finding needs a concrete failure scenario with inputs and the wrong result. A clean verdict is acceptable and useful. Priority P0 wrong physics or silent corruption, P1 wrong behaviour, P2 bounded defect, P3 hygiene.

DELIVERABLE: docs-private/reviews/2026-08-26-milestone-engine/magemin-status-fix.md. Verdict, counts, findings, then what you checked and found clean, then what you did not check.

Finish your final message with the single line COMPLETE: magemin-status-review
```
