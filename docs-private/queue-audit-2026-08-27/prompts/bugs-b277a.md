# Pinned prompt — `bugs-b277a`

- source: `prompt-file`
- prompt-file: `/tmp/goal-flight-501/dispatch/bugs-b277a.assembled.prompt` (EXISTS on disk)
- note: prompt_file taken from ledger.prompt_path
- pinned_at: 2026-08-27T14:54:06.319466+00:00

```
You have a steer mailbox at `$GOALFLIGHT_STEER_FILE`. Read it AT THE TOP OF EACH ITERATION and IMMEDIATELY BEFORE ANY git commit/push. Incorporate new messages into your plan; ack each with `!STEER-ACK: <seq>` on its own line; a steer may redirect or HALT you — honor it. If `$GOALFLIGHT_DISPATCH_SCRIPT` is set and you have nothing to do until the controller answers, use `python3 "$GOALFLIGHT_DISPATCH_SCRIPT" steer "$GOALFLIGHT_DISPATCH_ID" --wait --question-kind USER-NEED --timeout-secs <seconds> '<question>'` (or USER-CONFIRM) to emit the question and wait under a separate bounded deadline.

Your FULL original brief is at `$GOALFLIGHT_PROMPT_FILE`. Re-read it after any internal compaction/summarization, at the start of each long-run goal-loop iteration, and before final commit/exit; the disk file is authoritative over summarized memory.

Worker execution contract:
- Use your available tools to actually perform the requested filesystem, shell, research, or analysis actions before answering. Do not only plan, summarize, or describe commands.
- For successful completion, emit a final line outside any Markdown fence in this exact shape supplied by the dispatch-specific identity contract.
- The `!COMPLETE:` line must be the last non-empty line of your output. Do not print anything after it.
- Legacy unprefixed marker lines remain accepted; new emissions use the `!` prefix.

Terminal evidence identity contract:
- Every terminal marker payload starts with the exact dispatch id `bugs-b277a`.
- Successful final shape: `!COMPLETE: bugs-b277a — <summary>`.
- Use the same id prefix for READY, RESULT, FAILED, USER-NEED, USER-CONFIRM, or BLOCKED. A generic or foreign marker is ignored.

# DISPATCH b-277 — eleven copies of a refusal idiom make a false claim (fixer, goal-loop)

## 1. Situational frame
Fixer for the pm2 BUGS controller (`pm2-bugs`, lane `bugs`). The adjudication is
DONE and evidence-based — do not re-open it, and in particular **do not resolve
it by counting**. Read §4 before editing: this change stales six RAG cards and
there is a required repair step.

Create and work ONLY in your own worktree:
``​`bash
cd /Users/simonrowland/Repos/pm2
git worktree add /private/tmp/pm2-bugs-b277/pm2 -b fix/b-277 origin/main
``​`
The checkout MUST be named `pm2` under a slug parent — the repo root IS the
`pm2` package, so any other basename breaks imports.

GATE FORM (mandatory; `PM2_KILN_REPO` is NOT optional in a worktree — without
it `surrogate/warpx_ingest/_deck_contract.py` guesses kiln as a SIBLING of the
checkout, true in the main checkout and false here, and you will see ~33
unrelated artefact failures):
``​`bash
cd /private/tmp/pm2-bugs-b277/pm2
PM2_KILN_REPO=/Users/simonrowland/Repos/kiln \
PYTHONPATH=/private/tmp/pm2-bugs-b277 \
/Users/simonrowland/Repos/pm2/.venv/bin/python -m pytest -q -p no:randomly <targets>
``​`
PROVE THE TREE FIRST (must print YOUR worktree):
``​`bash
PYTHONPATH=/private/tmp/pm2-bugs-b277 /Users/simonrowland/Repos/pm2/.venv/bin/python -c "import pm2, os; print(os.path.dirname(pm2.__file__))"
``​`
`tests/rf/` is **T2 — STUDIO ONLY**: single named nodes locally are fine, the
suite gate goes to `scripts/ci-studio.sh rf` with `PM2_CI_PYTHON=3.12`. Never
push. Explicit-pathspec commits. `git grep -n`, never `grep -r`. `STATUS:`
every ~10 min.

## 2. The defect
A twelve-fold copied idiom across `rf/coupling/`. Eleven copies read:
``​`python
except FileNotFoundError:  -> MISSING_UPSTREAM_RECEIPT
except OSError:            -> IDENTITY_MISMATCH
except Exception:          -> IDENTITY_MISMATCH
``​`
`FileNotFoundError` IS an `OSError` subclass, so handler ORDER carries the
entire meaning, and every OTHER `OSError` — symlink loop (ELOOP), permission
denied, name too long, stale NFS handle, I/O error — lands on
`IDENTITY_MISMATCH`.

**That is a false statement of fact, eleven times.** `IDENTITY_MISMATCH`
positively asserts the file WAS read and its identity did not match. When the
read itself failed, nothing was obtained, so nothing was compared, so no
identity could mismatch. It also sends the next investigator to audit content
identity when the actual fault is that the reader never got the bytes.

## 3. The adjudication — SETTLED, do not re-derive, do NOT count
The twelfth copy, `tether_circuit_closure.py:471`, maps `OSError ->
MISSING_UPSTREAM_RECEIPT`. **THE TWELFTH IS RIGHT AND THE ELEVEN ARE WRONG.**

Established from how each class is used everywhere else, not by preference:
- `IDENTITY_MISMATCH` — 12+ producer sites in `rf/coupling/antenna_descriptor.py`,
  details uniformly `*_mismatch` (`schema_mismatch`, `owner_mismatch`,
  `ell_k_key_mismatch`, `envelope_order_mismatch`, `arithmetic_radius_mismatch`,
  `pattern_fill_mismatch`, …). Every one means **obtained both, compared,
  differed**. Not one describes an absence.
- `MISSING_UPSTREAM_RECEIPT` — 405 sites, details dominated by `*_required` /
  `*_missing` / `*_evidence_required`. Every one means **the thing that would
  establish this is absent**.

ELEVEN COPIES OF AN IDIOM ARE ONE DECISION COPIED ELEVEN TIMES, NOT ELEVEN
VOTES. If you find yourself reasoning "but eleven agree", stop — that reasoning
gives the wrong answer here and it is exactly what this dispatch exists to
avoid.

## 4. Fix contract
1. **COLLAPSE THE ARMS** in the eleven sites: map ALL `OSError` (including
   `FileNotFoundError`) to `MISSING_UPSTREAM_RECEIPT`, carrying
   absent-vs-unreadable in `detail`. `RefusalClass`'s own docstring says
   "lane-specific facts remain in `detail`". This is deliberately not a
   two-arm reorder: **with one arm there is no ordering to get wrong**, so the
   defect becomes unable to recur rather than merely corrected. Keep the
   `except Exception` arm's behaviour as-is unless you find it is also
   asserting a false fact — if so, report rather than widening scope.
2. **LEAVE `tether_circuit_closure.py:471` ALONE.** It is already correct.
   Removing the outlier to "make them consistent" would be the exact wrong fix.
3. Sites (verify each at HEAD before editing): `magnetosphere_riding.py:204,257`;
   `shaped_lift_scatter_inflation.py:461,531`; `shock_fold_modes.py:216,250`;
   `shock_lens.py:202,243`; `shock_lift.py:343,388,710`.
4. **PER-CONDITION TESTS**, one per condition, so the next copy cannot drift
   silently: file ABSENT → the expected class/detail; file present but
   UNREADABLE (simulate ELOOP or EACCES — a symlink loop is the realistic case
   and has fired three times in this repo today) → `MISSING_UPSTREAM_RECEIPT`;
   file read but DIGEST MISMATCH → `IDENTITY_MISMATCH`. Prove the unreadable
   case FAILS against the unfixed code.
5. **RAG CARD REPAIR — REQUIRED, AND READ THE RECEIPT.** All six modules are
   cited in `docs-private/rag/coupling/SOURCE-DIGESTS.json`, so editing them
   stales the citing cards' `evidence.derivation.digest` and the manifest, and
   `tests/rf/test_rag_corpus_honesty.py` will red. Repair with
   `rag_refresh_source_digests --only <path>` PER MODULE (b-284 added
   `--only`/`--card`). **A clean exit while known-stale pins are still held is
   itself a defect — report it, do not proceed past it.** Do NOT hand-edit any
   digest; the tool's own error message forbids it. Do NOT `--apply` without a
   selector: two W1-C pins are deliberately held stale as b-282 and applying
   them would launder unverified evidence.

## 5. Constraints + null hypothesis
- Do NOT weaken, reorder-only, or widen any refusal class, and do not add a new
  class. This is a mapping correction.
- Do not touch any module outside the five files in §4.3.
- NULL HYPOTHESIS you must address: *"`IDENTITY_MISMATCH` was chosen
  deliberately for unreadable files because the caller routes both to the same
  recovery, so the eleven are a considered convention and the outlier is the
  drift."* Refute or confirm from the CONSUMERS — find what actually branches
  on `refusal_class` and whether any consumer treats the two classes
  differently. If a consumer genuinely routes them identically the change is
  still correct (a false claim is still false) but the SEVERITY drops and you
  should say so. If you find a consumer that would be BROKEN by the change,
  STOP with `BLOCKED:` and the evidence.

## 6. Report
`RESULT:` + verdict, commit SHA, the eleven sites changed and confirmation the
twelfth was left alone, the three per-condition tests with the unreadable case
shown failing pre-fix, the `--only` receipt for each of the six cards (quoting
`pinned=`/what it left), your null-hypothesis refutation with the consumer
evidence, owning suites by full-repo grep, and the studio `rf` gate
(sha + host + `python=` + `pinned=`). `COMPLETE: b-277` last line.

```
