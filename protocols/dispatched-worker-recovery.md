# Dispatched-Worker Recovery Protocol

The controller-takeover pattern for when an ACP-dispatched worker reaches a
terminal blocked state before its chunk landed. This is recovery, not normal
operation — the canonical path is worker-completes-and-commits. Use this when
the worker's status JSON shows `state: blocked` (commonly `early_marker_cancelled`
on a permission request the runner cannot route) and you need to salvage the
work-in-progress without re-doing it.

**The chunk-2/3a/12 root cause was a fixable dispatch shape**: those workers
ran the gstack `/review` self-pass as a nested ACP tool-call (worker's
`execute_command` → codex-acp shim's permission gate). The canonical fix is
to invoke the review as a bash-tail subprocess with codex's own read-only
sandbox + bypass-approvals flags (see `protocols/chunk-review.md` §"How the
review runs"). That removes the permission-gate blocking class entirely.
Once chunk prompts adopt that pattern, this recovery protocol applies only
to other terminal-blocked cases (genuine destructive-op requests,
infrastructure failures, auth issues) — not the routine review-blocking
class.

## When this applies

The worker's status JSON shows:

- `state: blocked` or `state: failed` with `worker_alive: false`
- `last_event_kind` is `request_permission`, `response_error`, or a similar
  late-flow error
- The worker's dirty edits are visible on disk in `git status` (the worker
  did substantive work before blocking)

If the worker died early (events_seen < ~10, no dirty-tree changes), this is
NOT a recovery case — it's a re-dispatch case. Fix the dispatch (broader
allow-patterns, different mode, different agent) and re-fire the chunk.

## Steps

1. **Read the worker's status JSON.** Confirm `state` is terminal-blocked and
   capture the blocking detail (last `permission_pending` entry, error message,
   `text_excerpt` showing what the worker said it accomplished).

2. **Inspect the dirty tree.** `git diff --stat <worker's scope files>` shows
   the worker's pending edits. Verify they match the chunk's authorized scope
   (no out-of-scope file mutations, no forbidden patterns introduced).

3. **Run the verification gates the worker would have run.** Focused tests
   (`./tests/run.sh` or specific test targets per chunk scope), schema
   validation if relevant (e.g., JSON adapter manifests, YAML frontmatter),
   forbidden-pattern grep (the `test_instruction_split_contract` rule against
   protocols/scripts cross-referencing back to SKILL section anchors, plus
   the host-tool literal scan per `tests/bash/test-agent-adapters.sh`). These
   would have run in the worker if it hadn't blocked.

4. **Run an independent review on the worker's diff.** `gstack /review` via
   the host's normal skill-load path, OR `codex review` via bash-tail if the
   controller is non-native. The review runs controller-side as a read-only
   operation, NOT nested through the dead worker's ACP shim (see
   `protocols/chunk-review.md` §"Where the review runs"). Apply
   P3-safe-easy findings inline per the new policy.

5. **Stage the salvageable files explicitly.** `git add` the worker's scope
   files only — never `git add -A`. Leave unrelated dirty WIP for its own
   chunks.

6. **Commit with worker attribution.** Commit message should:
   - Use role descriptors per codename hygiene (no agent common names beyond
     existing tracked filenames)
   - Cite the chunk + plan reference
   - Acknowledge the worker did the implementation work (e.g., "Worker
     dispatched via the OpenAI-side ACP shim completed the implementation
     work and ran focused tests green, then blocked on <reason> — that step
     is replaced by the manual review pass landed in this commit")
   - Include `Co-Authored-By` for the controller (and worker, when its
     attribution surface is appropriate per the codename rule)

7. **Note in the goal-queue** that the recovery happened so the next session
   sees the pattern frequency. Multiple recoveries on the same blocking
   reason = strong signal to fix the dispatch path (e.g., the layer-2
   permission watcher in R26).

## What NOT to do

- **Don't re-dispatch the same chunk with the same allow-patterns and hope
  it works.** If the worker blocked on a known permission path, change the
  dispatch (add allow-patterns, switch to inline mode, use yolo per R26's
  documented workaround) before re-firing.
- **Don't extend the worker's scope by adding things you noticed while
  salvaging.** Recovery commits the worker's authorized work, no more.
  Adjacent fixes belong in their own chunks.
- **Don't skip the review step.** The fact that the worker did most of the
  work doesn't mean the work is correct. Run the controller-side review.
- **Don't silently drop the worker's self-review.** If the worker was about
  to run gstack `/review` when it blocked, the controller MUST run that
  review before commit — the worker can't, but the work isn't reviewed
  unless someone runs it.

## Related R-items

- **R26** (handoff backlog) — the underlying ACP permission-escalation bug
  that triggers the recovery pattern. The architectural fix (Haiku-subagent
  permission watcher, layer-2) replaces this recovery protocol with normal
  flow for the common cases.
- **R19** — the hand-rolled-review anti-pattern. The recovery's controller-
  side review path MUST use gstack `/review` (or the bundled fallback
  prompts), not a hand-rolled "please review this diff" prompt.
