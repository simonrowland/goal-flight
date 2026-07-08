"""Contract test: the review-cadence demand must stay CONCRETE and PRESENT.

Guards the regression class that shipped silently in the 1.2.1 terseness pass
(caught 2026-07-08): SKILL.md said milestone review runs on a "K-commit cadence"
with no concrete K, so the sweep never fired and controllers forgot it. The
golden-master + wc-budget tests could not catch this — golden-master PINS exact
text (it would have preserved the vague wording) and the budget test only caps
length. Neither asserts the *semantic* contract that review is actually demanded
on a concrete cadence.

These assertions are deliberately loose about exact wording/numbers (a future
editor may change 5 -> 6 or reword freely) but tight about the load-bearing
invariants: a concrete milestone cadence with a forcing nudge, a mandatory
per-chunk gate, a >=2 concern-diverse floor, three named review layers, and a
UNIVERSAL null-hypothesis in both the worker self-review and the controller
per-ticket gate. If any of these regress to vagueness or absence, this fails.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _milestone_context(text: str) -> str:
    """Lines mentioning the milestone cadence trigger, joined."""
    return "\n".join(
        line for line in text.splitlines()
        if re.search(r"milestone|cadence|since last (?:clean )?sweep", line, re.I)
    )


def test_milestone_cadence_is_concrete_not_an_undefined_placeholder():
    """The exact K=undef regression: a cadence trigger with no concrete count."""
    skill = _read("SKILL.md")
    proto = _read("protocols/milestone-review.md")

    # The historical broken form was a single letter used as an *undefined*
    # commit count. K-commit wording is allowed only when K is bound to a digit.
    for name, text in (("SKILL.md", skill), ("milestone-review.md", proto)):
        if re.search(r"\bK[- ]commit\b", text):
            assert re.search(r"\bK\s*=\s*\d", text), (
                f"{name}: reintroduced undefined 'K-commit' cadence placeholder — "
                "the milestone trigger must name a concrete number of commits/chunks"
            )

    # The milestone cadence must state a concrete integer count somewhere in its
    # trigger context (e.g. '5 committed chunks', 'default N=5'). No digit in the
    # cadence context == the trigger is undefined == it silently never fires.
    ctx = _milestone_context(skill) + "\n" + _milestone_context(proto)
    assert re.search(r"\d", ctx), (
        "milestone cadence has no concrete number in SKILL.md/milestone-review.md "
        "— an undefined cadence never fires (the 2026-07-08 regression)"
    )
    # And a defined single-letter variable is fine ONLY when bound to a digit
    # (e.g. 'N=5'); an unbound 'N-commit cadence' with no '= <digit>' nearby is
    # the same bug in a different letter.
    for m in re.finditer(r"\b([A-Z])[- ]commit(?:-worthy)?\b", ctx):
        letter = m.group(1)
        assert re.search(rf"\b{letter}\s*=\s*\d", ctx), (
            f"cadence uses '{letter}-commit' without binding {letter} to a "
            f"concrete number (e.g. '{letter}=5')"
        )


def test_milestone_review_has_a_forcing_nudge():
    """Environmental forcing: the count must be surfaced, not left to memory."""
    proto = _read("protocols/milestone-review.md")
    skill = _read("SKILL.md")
    assert re.search(r"since last (?:clean )?(?:milestone )?sweep", proto, re.I), (
        "milestone-review.md must describe the 'chunks since last sweep' counter"
    )
    assert re.search(r"since last sweep|chunks since", skill, re.I), (
        "SKILL.md core must reference the milestone-cadence nudge so a controller "
        "reading only the core knows the count is tracked"
    )


def test_per_chunk_review_is_mandatory_and_distinct_from_milestone():
    skill = _read("SKILL.md")
    assert re.search(r"milestone review is a separate gate", skill, re.I), (
        "SKILL.md must keep milestone review distinct from chunk review"
    )
    # per-chunk review stated as mandatory (not optional) for every commit-worthy chunk
    assert re.search(r"every commit-worthy chunk|reviews EACH returning chunk", skill, re.I), (
        "SKILL.md must state per-chunk review as mandatory for every chunk"
    )


def test_review_floor_is_at_least_two_concern_diverse():
    cr = _read("protocols/chunk-review.md")
    assert re.search(r"(?:>=\s*2|≥\s*2|at least two|two)\b.{0,40}concern", cr, re.I | re.S) or \
           re.search(r"concern.{0,40}(?:>=\s*2|≥\s*2|at least two|\btwo\b)", cr, re.I | re.S), (
        "chunk-review.md must state a >=2 concern-diverse review FLOOR"
    )


def test_three_review_layers_named_in_core():
    skill = _read("SKILL.md")
    for layer in ("self-review", "chunk review", "milestone review"):
        assert layer in skill.lower(), f"SKILL.md missing review layer: {layer}"


def test_null_hypothesis_is_universal_worker_and_controller():
    """Null-hypothesis must be a floor for EVERY patch, in BOTH gates — not
    reserved for a 'complicated' tier (user correction 2026-07-08)."""
    wc = _read("protocols/worker-contract.md").lower()
    cr = _read("protocols/chunk-review.md").lower()
    assert "null" in wc and "hypoth" in wc, (
        "worker-contract.md must require a null-hypothesis in the worker self-review"
    )
    assert "null" in cr and "hypoth" in cr, (
        "chunk-review.md must require a null-hypothesis in the controller per-ticket gate"
    )


def test_worker_prompt_surfaces_carry_null_hypothesis_floor():
    """The universal null check must be in prompts workers actually receive."""
    surfaces = {
        "prompts/dispatch-wrapper.md": _read("prompts/dispatch-wrapper.md"),
        "templates/codex-goal-prompt.md.tpl": _read("templates/codex-goal-prompt.md.tpl"),
    }
    for name, text in surfaces.items():
        lower = text.lower()
        assert "null-hypothesis" in lower or ("null" in lower and "hypoth" in lower), (
            f"{name}: actual worker prompt surface must carry the null-hypothesis floor"
        )
        assert "did not achieve" in lower and "no-op" in lower and "regression" in lower, (
            f"{name}: null hypothesis must name failure/no-op/regression outcomes"
        )
        assert "confirm" in lower and re.search(r"reject(?:s|ed|ing)?", lower), (
            f"{name}: null check must actively try to confirm the null and require evidence rejecting it"
        )


if __name__ == "__main__":
    import sys
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL  {name}: {exc}")
    sys.exit(1 if failures else 0)
