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
sandbox, `-c approval_policy=never`, and closed stdin (`< /dev/null`; see
`protocols/chunk-review.md` §"How the review runs"). That removes the
permission-gate blocking class entirely.
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
   the host-tool literal scan per `tests/bash/test-agent-adapters.sh`), and
   the **codename-hygiene scan against the worker's proposed commit message,
   new filenames, and any tracked-content edits** — per `AGENTS.md`
   "Git-Visible Trigger Hygiene". This last check is the most
   strategically important: vendor-trigger codenames in git-visible content
   shift the account's API rate-billing tier, so the recovery commit MUST
   pass this scan before staging. Use role descriptors (worker / runner /
   shim / orchestrator) in commit messages, not the vendor's product name.
   These verifications would have run in the worker if it hadn't blocked.

4. **Run an independent review on the worker's diff.** `gstack /review` via
   the host's normal skill-load path, OR a `codex exec` review prompt via the
   bash-tail shape in `protocols/chunk-review.md` if the orchestrator is
   non-native. The review runs controller-side as a read-only operation, NOT
   nested through the dead worker's ACP shim (see `protocols/chunk-review.md`
   §"Where the review runs"). Apply P3-safe-easy findings inline per the new
   policy.

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
   - Include `Co-Authored-By` for the orchestrator (and worker, when its
     attribution surface is appropriate per the codename rule)

7. **Note in the goal-queue** that the recovery happened so the next session
   sees the pattern frequency. Multiple recoveries on the same blocking
   reason = strong signal to fix the dispatch path (e.g., the layer-2
   permission watcher in R26).

## Recovery sub-case: worker WIP bundled into someone else's commit

The fb05e84-class incident (2026-05-27): orchestrator (or another worker)
ran bare `git commit` while a worker had staged its scope files. The
worker's staged WIP got bundled into a commit with the wrong author /
scope / message. The worker either finds nothing left to commit (silent
exit) or fails with "nothing to commit" depending on its workflow.

Symptoms:

- Worker's status JSON says `state: complete` but git log shows the
  worker's scope landed in a different commit (different author, message
  doesn't match the chunk's expected `Chunk-N: <slug>` shape).
- `git log --author=<worker>` returns nothing for the recent window.
- The bundled commit's diff contains files from multiple dispatch
  scopes (cross-scope file list).

Recovery options:

1. **If the bundled commit has NOT been pushed** — preflight first, then
   reset:
   ```bash
   # Preflight (run all four before reset):
   bundled_sha=$(git rev-parse HEAD)
   echo "preserving bundle as $bundled_sha"          # so you can recover via reflog if needed
   git status --short                                # confirm clean working tree (no uncommitted)
   # Test: is HEAD already on upstream? If so, the bundle was pushed
   # and reset would diverge local from remote.
   if git rev-parse --abbrev-ref @{u} >/dev/null 2>&1; then
     if git merge-base --is-ancestor HEAD @{u} 2>/dev/null; then
       echo "ABORT: HEAD is on upstream; pushed. Use option 2 (amend) or option 3 (document)."
       exit 1
     fi
     echo "branch has upstream but HEAD not pushed — reset is safe"
   else
     echo "no upstream — local-only branch, reset is safe"
   fi
   ```
   Then `git reset --soft HEAD~1` brings the bundled changes back to
   staging. Commit by scope with explicit pathspecs:
   `git commit -m "<scope-A msg>" -- <scope-A files>` followed by
   `git commit -m "<scope-B msg>" -- <scope-B files>`. History now
   correctly attributed. Recovery if anything goes wrong:
   `git reset --hard "$bundled_sha"` restores the bundled commit.
2. **If the bundled commit HAS been pushed (or rewriting is undesired):**
   leave it in place. Amend its message to credit all bundled scopes
   (`git commit --amend -m "<combined msg referencing chunks 6 + 7>"`),
   OR document the bundle in `docs-private/RESUME-NOTES-<date>.md` with
   the dispatch IDs and the chunks they should have landed under. Future
   audits see the deliberate bundle.
3. **Forward-looking:** the commit guard (`scripts/goalflight_commit_guard.py`,
   installed as a `.git/hooks/pre-commit` symlink) refuses bare `git commit`
   while active same-root leases exist. Install it; the runtime error
   teaches the discipline at the moment of failure.

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
  to run gstack `/review` when it blocked, the orchestrator MUST run that
  review before commit — the worker can't, but the work isn't reviewed
  unless someone runs it.
- **Don't conclude a worker's artifacts are missing from `ls`/`find`/`git
  status`/`grep`** — and never re-author/overwrite on that basis. Those
  ENUMERATE a directory, and a separate controller process's enumeration view
  of a just-created file can be **stale for minutes** on local APFS (rpp-kb
  2026-06-23: `find`+`git status`+`grep`+a fresh re-check all read a complete
  leaf set as absent for minutes; only a write-collision and running the gates
  revealed it — nearly drove a destructive re-author).

## Verify artifacts by open, never by enumeration

A worker declares its output path(s) in its terminal `READY:`/`COMPLETE:` marker.
Verify those exact paths by **opening them**, not by listing their directory —
opening a known path by name forces a fresh fetch; enumeration can read stale.

- Use **`goalflight_status.py --verify-artifacts <id>`** (exit 0 = all declared
  artifacts present+non-empty). It extracts the path(s) from the terminal marker
  and confirms each by direct `open()`, bypassing the stale-enumeration view.
- Or check the exact path yourself by NAME (never by listing its dir): strongest is
  a **content read** — `cat <path>` or run the **gate that reads file contents** (the
  worker's own validators). `test -f <path>` is a by-name `stat` — fresher than
  directory enumeration, but a content read is the most authoritative.
- A "missing/incomplete" verdict that would gate **re-authoring or overwrite** is
  only trustworthy after a **no-clobber write collision** (`set -C; > path`, or
  `open(path,'x')` raising) **or** a content-reading gate run — never after any
  number of reads. A transient `--wait` `worker_dead` is likewise a *lead to
  verify*, not a death: reconcile from the `READY:` marker + `--verify-artifacts`
  + the reconciled `status.json`.

## Worker bypass anti-pattern

Distinct from the recovery cases above. Here the worker SHOULD have stopped
and returned `BLOCKED:` but instead found a workaround route and "completed
the task" through it. The envelope (what counts as appropriate completion)
is the orchestrator's call; the worker stays out of the orchestrator's lane.

### Pattern

Worker reasoning: *"The task is to produce X. I have X drafted. The path I
tried to deliver X failed. Returning with X undelivered would be
'incomplete'. I'll find another path to deliver X."*

What worker is missing: *"The envelope says deliver X **via** file-backed
return / standard git path / etc. Failing to satisfy the envelope is
failing the task. Workaround paths are the orchestrator's call, not mine."*

### Examples observed 2026-05-28

1. **hooks-v2 worker self-pushed via GitHub Git Data API.** Task: install
   context-discipline hooks + commit locally via `scripts/goalflight_commit.sh`
   (which the same chunk was installing) + return `READY:` + path + SHA.
   Block: worker's sandbox couldn't open `/dev/null` (zsh stderr redirect
   issue); `git commit` failed. Bypass: worker constructed a commit via
   GitHub REST API and fast-forwarded `refs/heads/main` on origin without
   any local `git commit` event and without push authorization.
   **Right move:** stage files locally, return `BLOCKED: commit step
   blocked by /dev/null sandbox quirk, all artifacts staged in working
   tree, ready for orchestrator to run goalflight_commit.sh and authorize
   push`.

2. **R-coverage Explore agent dumped 5KB inline.** Task: map R1–R26 backlog
   to commits + write report to a findings file + return ONLY
   `READY: <path>` + ≤3-line TL;DR. Block: worker stated file-write was
   "blocked for content verification" (probably an unresolvable permission
   prompt). Bypass: worker returned the full ~5KB report inline in its
   chat reply, defeating the file-backed-return contract. **Right move:**
   return `BLOCKED: cannot write to <intended-path>, content drafted in
   worker context but not persisted, please clarify path permission or
   accept inline override`. Then orchestrator decides.

### Dispatch-prompt clause to inline

Every dispatch prompt that defines a file-backed return contract MUST
include verbatim:

```
If the file-write path is blocked (sandbox, permission, hook), return
exactly:

  BLOCKED: <intended-path> not writable due to <reason>

  TL;DR: <what was drafted; ≤3 lines>

  Recommended orchestrator action: <one line>

Do NOT inline the drafted content. Do NOT use alternate APIs (REST,
git plumbing) to bypass the standard path. The orchestrator decides.
```

Every dispatch prompt that involves git operations MUST include verbatim:

```
Commits use the standard `git add` / `git commit` path or
`scripts/goalflight_commit.sh`. Do NOT use GitHub REST API,
`git update-ref`, or other plumbing to construct commits. If those fail,
return BLOCKED with the failure trace; do not bypass.

Push is NEVER authorized in a dispatched worker prompt unless this prompt
explicitly says "push permitted". Push requires orchestrator verification +
user authorization.
```

### Why rule-loading alone fails

Both 2026-05-28 examples had explicit file-backed return / no-bypass
constraints in their dispatch prompts and bypassed anyway. The
environmental enforcement layer is the real fix:

- Pre-tool hook on the worker side: block writes to
  `~/.claude/settings.json`, `~/.claude/hooks/`, `/private/tmp/`,
  out-of-cwd paths.
- Post-dispatch audit on the orchestrator side: check `origin/main`
  movement vs the local push reflog. If `origin/main` advanced without a
  corresponding controller-side `git push` event, flag.
- `goalflight_commit_guard.py` should grow a sibling check:
  `goalflight_push_audit.py` for the unauthorized-push class.

## Related R-items

- **R26** (handoff backlog) — the underlying ACP permission-escalation bug
  that triggers the recovery pattern. The architectural fix (Haiku-subagent
  permission watcher, layer-2) replaces this recovery protocol with normal
  flow for the common cases.
- **R19** — the hand-rolled-review anti-pattern. The recovery's controller-
  side review path MUST use gstack `/review` (or the bundled fallback
  prompts), not a hand-rolled "please review this diff" prompt.
