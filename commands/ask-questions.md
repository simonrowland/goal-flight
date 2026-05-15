# ask-questions [--scope <area>]

Spawn read-only subagents to anticipate integration / requirements / snags questions. Surface only the ones with enough context to make sense to the user. Goal: clear ambiguities up front so the 12-hour unattended run doesn't stall on user input.

## Modes

- **Standalone**: `/goal-flight ask-questions` — user invokes explicitly between decompose-plan and execute (or any time).
- **Auto-chained**: invoked at the end of `decompose-plan` with `--scope decomposition`.
- **Continuous**: invoked from inside `execute` when the controller hits an ambiguity that requires user input.

## Steps

### 1. Pick scope

| Scope arg | Anticipator focus |
|-----------|-------------------|
| `decomposition` (default after decompose-plan) | Cross-chunk dependencies, ambiguous SCOPE/ACCEPTANCE wording, missing FORBIDDEN constraints |
| `<area>` (e.g., `auth`, `db-migration`) | Area-specific chunks; ignore unrelated work |
| `current-chunk` (continuous mode) | The chunk currently being executed; surface only blockers |
| `next-chunks` (continuous mode, look-ahead) | Next 1-2 upcoming chunks; non-blocking surfacing |

### 2. Spawn 1-2 anticipatory subagents

**Read-only Explore subagents** with the prompt at `prompts/ask-anticipatory.md`. Substitute the scope and paths. Each subagent:

1. Scans the goal-queue, AGENTS.md, any binding-spec, recent git log.
2. Generates `(question, default-answer, confidence, risk-if-wrong, second-opinion-suggestion)` tuples.
3. For low-confidence items: notes "consider second opinion from <other model>" — codex if Claude is uncertain; claude if codex is uncertain.

If two subagents are spawned (recommended for `decomposition` scope; one is sufficient for `current-chunk`), run in parallel and dedupe results.

### 3. Filter aggressively

Drop:
- Questions with answers obvious from the scan (controller defaults silently).
- Questions that need user knowledge they can't reasonably have yet (too early — defer to a later ask-questions call).
- Questions without enough context for the user to answer in <30 seconds without re-explanation.
- Duplicates from the two agents.

Keep:
- Questions where the default answer is non-trivial AND the wrong default would compound across chunks.
- Confidence: medium-low.
- Risk-if-wrong: high.

### 4. Second-opinion sweep for uncertain defaults

If a subagent flagged "consider second opinion" for an item that you'd otherwise default on:

- If both codex and Claude reviewers are available: spawn a quick reviewer of the other type with just that item. Compare answers. If they agree, default silently. If they disagree, escalate to user.
- If only one model is available: ask the user.

### 4.5. Dual-subagent planning for open architectural questions

If anticipator surfaced a question that's bigger than a default-or-ask — an open architectural question where the right answer needs thought rather than user knowledge — consider the dual-subagent adversarial planning pattern (`prompts/dual-plan-adversarial.md`). Dispatch two planners in parallel with different lenses (chemistry-first vs engineering-first, performance-first vs correctness-first, etc.); each writes a plan document; controller synthesizes. Cheaper for context than one large planner; the adversarial divergence is the value.

Use this sparingly — only for questions that genuinely benefit from two complementary perspectives. For most ambiguities, the single-anticipator + default-or-ask flow is enough.

### 5. Present to user

Use `AskUserQuestion`. Max 4 questions per call (group related; if more remain, present in batches). For each:

- **Question**: phrased so a user can answer in <30 seconds.
- **Options**: the controller's recommended default first (label it "(recommended)"), with a one-line rationale in the description.
- **Header**: short noun-phrase (3-5 words).

If you have 5+ questions: ask the most important 4 first, then defer the rest until after the user answers (or until a natural pause in execute).

### 6. Record answers

Append to `<repo-root>/docs-private/<topic>-answers-<today>.md` (create if not present). Include for each:

```
## Q<N>: <question>
- **Asked**: <iso-timestamp>
- **Scope**: <decomposition | <area> | current-chunk | next-chunks>
- **Source**: anticipatory subagent (codex|claude|both) / mid-execute pause
- **Default proposed**: <controller's default>
- **User answered**: <answer or "accepted default">
- **Affects chunks**: <list of chunk numbers/slugs>
```

Also: if the answer changes any chunk's SCOPE/CHECKLIST/ACCEPTANCE/FORBIDDEN, edit the goal-queue inline and append a margin note like `<!-- amended <iso> per Q<N> -->`.

### 7. Mid-execute behavior (continuous mode)

When invoked from inside `execute`:

- **Block the loop only if** the answer is required for the current chunk's safe completion (e.g., "should I drop the column or rename it?" — destructive choice the user must make).
- **Otherwise non-blocking**: queue the question in the answers file and continue. Address at the next natural pause (after current commit, or before next milestone review).
- **Look-ahead questions**: spawned by the post-commit look-ahead subagent. These are always non-blocking; queue them for the next standalone ask-questions invocation.
