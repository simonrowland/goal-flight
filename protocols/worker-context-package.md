# Worker context package (lane pinning)

Workers are codex/grok; they do not get smarter when the controller does, and they
load NOTHING the dispatch prompt does not carry or point at. When a project's
correctness laws live in prose specs, workers (and their reviewers) will repeatedly
regress the same subsystem while reporting green. Dated incident (2026-07-07,
downstream project): multiple high-effort workers and their reviewers repeatedly
inserted per-element procedural code into a vectorized hot loop and shipped
structurally impossible outputs, because the domain invariants lived in spec
documents no worker ever loaded. The fix that worked — and that this protocol
generalizes — was pinning a per-lane context package into every dispatch.

**A lane** = a subsystem + the family of chunks that touch it (e.g. "the solver hot
path", "the ingest schema"). Pinning is per-lane, not per-project: most lanes need
nothing beyond the standard five-layer briefing.

## Triggering signals — when a lane needs a package

The controller MUST evaluate these at `init`, at `decompose-plan`, and again before
every `execute` wave that dispatches into the lane (signals appear as work reveals
them; a lane clean at init can trip a signal mid-run):

| Signal | Test |
|---|---|
| Hot-path / perf invariants | Does the lane contain code where per-call cost is a correctness property (tight loops, vectorized kernels, latency budgets)? |
| Spec-resident correctness laws | Do domain laws (physics, accounting identities, protocol grammars, tolerances) live in prose/specs rather than in types or failing tests? |
| Regression history | Has ANY worker or reviewer previously regressed this subsystem while reporting success? One occurrence is the signal; do not wait for two. |
| Shared seam under parallel load | Will this wave put ≥2 workers into chunks that touch one seam (same module, schema, or API surface)? |

Any YES → the lane needs a context package, and the controller builds or refreshes
it BEFORE dispatching. Dispatching into a triggered lane without a package is the
documented failure mode, not a judgment call.

## Package contents

Keep the package in the project (`docs-private/lanes/<lane>/` or the project's
equivalent), one directory per lane, four parts:

1. **Lane brief** (`brief.md`, ≤1 page, prepended VERBATIM to every dispatch prompt
   for the lane — never summarized, never linked-instead-of-pasted):
   - load-path narrative: how data/control actually flows through the lane, in the
     order the worker will encounter it;
   - forbidden-moves table, each row citing the spec/test/incident that makes it
     forbidden (a bare "don't" gets argued with; a cited "don't" gets obeyed);
   - error taxonomy: the lane's failure modes and what each one looks like in output;
   - acceptance criteria as RUNNABLE commands, not prose ("`pytest tests/kernels -k
     invariants` green", not "keep the kernels correct").
2. **Ground-truth spec** (`spec.md`): requirements + pseudocode for the invariant
   behavior, carrying an **arbiter clause** — "if your code disagrees with this
   pseudocode, your code is wrong; STOP and report, do not reinterpret" — and a
   **change-control rule**: workers NEVER edit specs, tolerances, or golden values
   to make a check pass; a worker who believes the spec is wrong emits `BLOCKED:`
   with evidence and returns.
3. **Guard-test list** (`guard-tests.md`): the named test IDs that pin the lane's
   invariants, with **RED-first discipline** — a fix chunk must show the guard test
   failing before the fix and passing after; a green-only report is inconclusive.
4. **Ticket recipe** — the dispatch-prompt template for lane chunks (below).

## Pin durability — workers compact too

A verbatim prepend is necessary but not durable: on long runs the worker's OWN
session can compact (codex auto-compaction) or simply decay attention to turn-1,
and a lossy summary flattens exactly the high-value content — forbidden-moves
tables, citations, arbiter clauses — into "follow the spec". Defend in the
environment, not in the worker's memory:

- **The brief is a file first.** Prepend it verbatim AND cite its stable path in
  the prompt; the dispatch brief itself is also exposed as
  `$GOALFLIGHT_PROMPT_FILE`. A compacted summary loses a 1-page table but
  reliably preserves a short path; a file can be re-read, a prepend cannot.
- **Standing re-read instruction** in every pinned-lane prompt: re-read the brief
  and spec paths at the start of each goal-loop iteration, after any internal
  compaction, and before the commit gate.
- **Prompt delivery by file** (`--prompt-file`) for any pinned-lane or
  likely-long dispatch, and always when prompt text exceeds ~2KB — inline argv
  prompts are unrecoverable after compaction and fragile to quoting/length.
- **Memory-proof exit condition:** default likely-long chunks to the goal-loop
  shape (loop to tests-green + review convergence). The named guard tests are
  the exit gate a worker cannot forget its way past: RED-first evidence plus
  controller-side re-verification means a worker that lost the spec still
  cannot leave green without re-satisfying it.
- Known gap: the codex goal-loop iterates inside ONE session, so iteration
  boundaries are not automatic re-injection points — the re-read instruction
  above is the current defense; a harness-driven external iteration loop
  (fresh worker per cycle, re-seeded with brief + spec + diff + last test
  output) is the stronger future primitive.

## Ticket recipe (dispatch-prompt template for pinned lanes)

One ticket = one spec block + its named test IDs. The prompt for each such chunk:

- **QUOTES the ground truth** — paste the relevant spec block and pseudocode into
  the prompt; a pointer ("see spec.md") is a broken pin, workers under sandbox or
  focus pressure skip it;
- requires **RED-first evidence** in the worker's report (named guard test shown
  failing pre-fix);
- carries **scope guards that NAME the adjacent seams** not to touch, with
  STOP-and-report (`BLOCKED:`) instead of improvisation at the boundary;
- states **honest-outcome acceptance**: the deliverable is "the fix, OR the next
  honest reason it fails" — so a worker cannot tune numbers, widen tolerances, or
  special-case tests to force green;
- states **sanctioned-edit carve-outs** explicitly (which files/values the worker
  MAY change), so the change-control rule cannot be read as "touch nothing" and
  paralyze the chunk;
- is delivered **by prompt file** with the brief/spec paths cited, and carries the
  **standing re-read instruction** (§Pin durability) so the pins survive the
  worker's own compaction on long runs.

## Controller alerting — this protocol is a gate, not a reference

The discipline failed in practice precisely because it relied on the controller
remembering. Therefore:

- `init` inventories candidate lanes (signals table) and records the verdict per
  lane in the state skeleton — including explicit "no package needed" verdicts, so
  the next controller sees a decision, not a gap.
- `decompose-plan` marks each chunk with its lane; chunks in a triggered lane get
  the ticket recipe applied at decompose time, not at dispatch time.
- `execute` refuses (self-check, before each wave): *"for every chunk in this wave
  in a pinned lane — does the prompt prepend the lane brief verbatim and quote its
  ground truth?"* If no: fix the prompt before dispatch. If the package is missing
  or stale (spec changed since brief written): build/refresh it first; that is the
  wave's first chunk, not a deferrable chore.
- Reviewer dispatches into a pinned lane get the SAME brief prepended — the
  observed incident included reviewers approving structurally impossible outputs
  for want of the same context the executors lacked.
