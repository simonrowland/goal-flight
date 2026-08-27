# Review — t-367 attention-marker discriminator and t-368 deadline test

Read-only review of `32aa8b6` and `f390690` on branch `t367-discriminators`
at `/Users/simonrowland/Repos/goal-flight/worktrees/t367-discriminators`.
Brief: `$GOALFLIGHT_PROMPT_FILE` (`docs-private/briefs/rev-t367.md`).
Source was not edited; a temporary revert of `_retry_journal_busy` was
applied, the rewritten test was run, and the file was restored
(`git checkout -- scripts/goalflight_controllers.py`).

**Verdict: CLEAN.** Severity: P0 0 · P1 0 · P2 0 · P3 0.

All five attacks were executed against running code in a fresh `mktemp -d`
tree (`GOALFLIGHT_JOURNAL_DIR`, `GOALFLIGHT_STATE_DIR`,
`GOALFLIGHT_WAKE_LEDGER`, `GOALFLIGHT_MESSAGES_DIR`,
`GOALFLIGHT_TASK_STORE`, `GOALFLIGHT_PIDFILE_DIR`,
`GOAL_FLIGHT_PIDFILE_DIR`, `GOALFLIGHT_CAPACITY_CONF=/dev/null`). Live
ledger was not used.

---

## Claims under test

1. Hyphenated technical terms abutting an identity dash BIND when the
   leading token has no ledger record (`32aa8b6`).
2. An unreadable or missing ledger must BIND, not classify every
   hyphenated token as foreign (SC-153).
3. A genuine foreign dispatch id in identity-contract form must still
   NOT bind.
4. `test_retry_deadline_checked_before_subsequent_attempt` must fail if
   the production deadline check it pins is reverted (`f390690`).
5. No deleted/skipped/xfail tests; count must not fall; tolerances must
   not widen.

---

## 1. Does the rule now fail toward binding?

**OBSERVED: yes.** All five measured drop shapes bind on an empty isolated
ledger, for every attention kind (`BLOCKED`, `FAILED`, `USER-NEED`,
`USER-CONFIRM`) — 20/20.

Rule: `_attention_payload_names_foreign_dispatch`
(`scripts/goalflight_watch.py:1548-1587`) returns True only when ALL of:
leading token looks like a dispatch id (`-` plus charset,
`:1525-1526`), remainder after `lstrip(" \t")` starts with em/en dash
(`:1584-1586`), AND `_token_is_known_dispatch_id` finds a ledger file
(`:1529-1545`, `:1587`). Otherwise the attention marker binds
(`_terminal_marker_matches_dispatch` `:1622-1623`).

| Payload (kind=`BLOCKED`, expected=`wake-fold`) | Binds |
|---|---|
| `utf-8 — output is not decodable` | yes |
| `read-only — cannot write sandbox path` | yes |
| `pre-commit — hook refuses the chunk` | yes |
| `file-not-found — input fixture missing` | yes |
| `end-to-end — smoke run aborted` | yes |
| `t999-never-dispatched — needs controller` (id-shaped, unknown) | yes |
| `read-only sandbox — cannot write` (two-word) | yes |
| `pre-commit hook failed` (hyphenated, no identity dash) | yes |
| `foreign package unavailable` | yes |
| empty payload | yes |

Covered in-tree by `tests/python/test_terminal_vocab.py:566-616`.
Independently re-run via isolated pytest (that test passed) and a probe
that called `_terminal_marker_matches_dispatch` directly.

### Shape that still drops — should it?

**OBSERVED remaining drops** (attention `BLOCKED`, after writing a real
ledger record for `other-live-id`):

| Payload | Binds | Should it drop? |
|---|---|---|
| `other-live-id — needs controller` (em dash) | no | **yes** — identity-contract foreign form |
| `other-live-id – needs controller` (en dash) | no | **yes** — same separator set (`:1585`) |
| `other-live-id—needs controller` (no spaces) | no | **yes** — `_payload_leading_token` splits on the dash (`:1512-1522`) |
| `other-live-id<tab/spaces>— needs` | no | **yes** — `lstrip(" \t")` then em dash is still the instructed form |

Those are the discrimination working, not leftovers of the old shape guess.

**OBSERVED remaining binds of a *known* foreign token** (fail-toward-binding
by design, `scripts/goalflight_watch.py:1570-1572`): id-only
(`BLOCKED: other-live-id`), space-separated prose, ASCII `-` / `--`,
colon, pipe, NBSP-then-em, figure dash, minus, horizontal bar. None of
those is the identity-contract separator, so they bind even when the
ledger knows the token. That is the stated asymmetry (lost escalation >
extra wake).

**OBSERVED coincidence drop.** After `write_record` for dispatch id
`utf-8`, `BLOCKED: utf-8 — output is not decodable` no longer binds.
Same for a garbage file at `runs.d/read-only.json` with no JSON schema
(`is_file()` is the lookup, `:1542-1543`). Should those drop?

- A *real* dispatch named `utf-8` is indistinguishable from the identity
  contract. Dropping is the correct answer to attack 3.
- Auto ids are `{agent}-{pid}-{timestamp}`; instructed ids are
  kebab-case. Colliding with `utf-8` / `read-only` requires an operator
  to pass that `--dispatch-id` (or plant a file in `runs.d`).
- **HYPOTHESISED, not observed in production:** leftover historical
  records could shadow a hyphenated prose token until GC. That is
  inherent to "known = file exists", not a regression of the bind-unknown
  rule.

Parser path does not embed `dispatch_id` on scraped attention markers
(`parse_own_signal_attention_line` returns only `line`/`kind`/`text`,
`scripts/goalflight_terminal.py:151-155`). The `if embedded: return False`
branch (`goalflight_watch.py:1618-1621`) is not on the tail-scrape path.

---

## 2. What happens when the ledger lookup fails?

**OBSERVED: unknown / unreadable BIND.** `_token_is_known_dispatch_id`
(`scripts/goalflight_watch.py:1542-1545`):

```python
try:
    return goalflight_ledger.record_path(token, create=False).is_file()
except (OSError, RuntimeError):
    return False
```

`record_path(..., create=False)` (`scripts/goalflight_ledger.py:431-432`)
does not mkdir. Lookup is read-only.

Isolated probes, token `other-live-id — needs controller` after a record
was written in a *different* reachable dir:

| Condition | `_token_is_known_dispatch_id` | Attention binds | Exception |
|---|---|---|---|
| Record present, STATE_DIR readable | True | no | none |
| STATE_DIR missing (test `:635-645`) | False | **yes** | none |
| `chmod 000` STATE_DIR (dir exists, contains the foreign record) | False | **yes** | none |
| `chmod 000` `runs.d` | False | **yes** | none |
| STATE_DIR is a regular file | False | **yes** | none |
| `record_path` raises `OSError` / `PermissionError` / `RuntimeError` | False | **yes** | caught |
| `is_file()` raises `PermissionError` | False | **yes** | caught |

The unhealthy case (cannot tell whether a record exists) does **not**
re-create the old drop. Missing and unreadable *directories* fail toward
binding.

**OBSERVED, not a bug:** `chmod 000` on the record *file* still
`is_file()==True` (stat does not need read permission). That token stays
"known". This is existence, not content readability. It does not make
*everything* look foreign; it keeps one name classified as a dispatch.

**HYPOTHESISED residual:** `except (OSError, RuntimeError)` does not
catch `ValueError`/`TypeError`. Injecting those raised out of the helper.
No realistic trigger after the charset gate (`_DISPATCH_ID_SHAPE_RE`
`:1488`, ASCII only) — `Path` NUL and `encode()` surrogates cannot appear
in a token that passed step 1. Not filed.

In-tree coverage: missing STATE_DIR at
`tests/python/test_terminal_vocab.py:635-645`. Chmod/unreadable-dir was
not in the unit test; confirmed by probe.

---

## 3. Does a genuine foreign dispatch id still not bind?

**OBSERVED: identity-contract form does not bind.** After writing
`codex-12345-1700000000` (auto-id shape) as a real ledger record:

| Form | Binds |
|---|---|
| `BLOCKED: codex-12345-1700000000 — needs controller` | **no** |
| `!BLOCKED: …` (sigil; parser strips it, same `text`) | **no** |
| `BLOCKED: wake-fold — needs controller` (own id) | yes |
| `BLOCKED: cannot write sandbox path` (prose) | yes |
| `BLOCKED: codex-12345-1700000000` (id-only, no dash) | yes (rule 2, fail toward binding) |

All four attention kinds match. Parsed via
`parse_own_signal_attention_line` then
`_terminal_marker_matches_dispatch`. In-tree:
`test_terminal_vocab.py:618-633` (`t998-sibling-worker`) and
`test_terminal_vocab.py:475-480` / `:544-562` (`other-live-id` with a
written record); same contract in
`tests/python/test_ci_mutation_guards.py:107-183`.

Discrimination cuts both ways for the instructed
`<KIND>: <dispatch-id> — <summary>` form. It is not "bind everything".

---

## 4. Is the rewritten deadline test genuinely discriminating?

**OBSERVED: yes.** Production fix lives in
`scripts/goalflight_controllers.py:137-169` (`e60749f`): attempt 2+ is
not started once `now >= deadline`, including a post-sleep re-check at
`:163-165`. Bug shape: attempt 1 finishes *under* budget, backoff
`min(FLEET_JOURNAL_BUSY_BACKOFF_S, remaining)` (`:111`, `:162`) crosses
the deadline, attempt 2 starts anyway.

Rewritten test: `tests/python/test_controller_fleet.py:1434-1488`. Fake
`monotonic`/`sleep` (`:1467-1480`); attempt 1 costs 5ms against a 40ms
budget; remaining 35ms < 50ms backoff so sleep is exactly remaining.

### Revert experiment (production file, then restored)

Temporarily replaced the loop in `_retry_journal_busy` with the
pre-`e60749f` body (deadline checked only *after* an attempt). Isolated
pytest:

```
FAILED test_retry_deadline_checked_before_subsequent_attempt
AssertionError: 'busy after 2 attempts over 45ms'.startswith('busy after 1 attempts')
```

at `tests/python/test_controller_fleet.py:1486`. Arithmetic: 5ms attempt 1
+ 35ms sleep + 5ms attempt 2 = 45ms — attempt 2 ran. File restored;
re-run passed.

### Predecessor was inert — OBSERVED on a fake clock

Same construction with attempt cost 50ms vs 40ms budget (the old test's
`time.sleep(0.05)` shape):

| Implementation | Attempt cost | Result | Old assertion (`len(calls)==1` + `busy after 1`) |
|---|---|---|---|
| current | 50ms (over budget) | 1 attempt, no sleep | **pass** |
| pre-`e60749f` | 50ms (over budget) | 1 attempt, no sleep | **pass** |
| current | 5ms (under budget) | 1 attempt, sleep 35ms | pass (and new assertions pass) |
| pre-`e60749f` | 5ms (under budget) | **2 attempts** | **fail** |

The over-budget pairing trips the post-attempt check that already existed
on the buggy code. The under-budget pairing is the one that requires the
post-sleep re-check.

### Timing margins and load

**OBSERVED:** the test clock is fake (`:1462-1472`). `sleep(d)` advances
by exactly `d`. Machine load cannot change the outcome. `pytest.approx(0.035)`
(`:1488`) is default ~1e-6 relative, not a widened wall-clock window. The
removed `elapsed < 0.2` bound was looser than the bug (45ms < 200ms would
still have passed on reverted code if call-count were not asserted).

0.04s budget is the constant the inert predecessor used; 0.005s attempt
cost is modeled. The docstring (`:1453-1457`) proves any attempt cost in
`(0, 40ms)` leaves remaining `< 50ms` backoff, so sleep always lands on
the deadline. That inequality is the margin, not a measured 5ms. Fake
clock is the load-survival strategy.

**HYPOTHESISED (docstring only, not re-run here):** "real-timed
reproduction … 25ms first attempt … finished in 43ms with `busy after 1
attempts`" (`:1457-1458`). Not needed to establish discrimination; the
revert failure is.

---

## 5. Did anything get weakened?

**OBSERVED: no.**

| File | `d306c44` `def test_` | HEAD | skip/xfail in source |
|---|---|---|---|
| `tests/python/test_terminal_vocab.py` | 12 | **13** | 0 |
| `tests/python/test_controller_fleet.py` | 35 | 35 | 0 |
| `tests/python/test_ci_mutation_guards.py` | 11 | 11 | 0 |
| `tests/python/test_marker_sigil_and_signoff.py` | 9 | 9 | 0 |

`git diff --stat d306c44..HEAD -- tests/`: three files, `+167 / -12`, no
test file deleted. Isolated `pytest --collect-only` of the four files:
**68 collected**, matching the orchestrator gate. Isolated runs:
`test_terminal_vocab.py` + `test_ci_mutation_guards.py` +
`test_marker_sigil_and_signoff.py` + the deadline test = 34 passed;
full `test_controller_fleet.py` = 35 passed (46.3s).

Assertions tightened, not loosened: unknown hyphenated tokens must now
bind (`test_terminal_vocab.py:596-603`); foreign-id cases write a real
ledger record instead of trusting a shape guess; wall-clock `elapsed < 0.2`
replaced by exact fake-clock `calls == [100.0]` and `sleeps == [0.035]`.

---

## Test evidence (isolated env)

- `/opt/homebrew/bin/python3 -m pytest` on the four named modules:
  68 collected; focused subset 34 passed; fleet file 35 passed.
- Probe script `/tmp/rev-t367-probe.py` (not in-tree): measured shapes,
  remaining drops, chmod/unreadable ledger, genuine auto-id, fake-clock
  old-vs-new retry loops.
- Revert: production `_retry_journal_busy` → pre-`e60749f` loop →
  rewritten test FAILED `busy after 2 attempts over 45ms` →
  `git checkout -- scripts/goalflight_controllers.py` → test PASSED.

Worktree `git status` after restore: clean. No push.

---

## Verdict

CLEAN. The marker rule fails toward binding, including when the ledger
directory is missing or unreadable; a genuine foreign id in identity-contract
form still does not bind; the rewritten deadline test fails on the reverted
production loop and passes on HEAD.
