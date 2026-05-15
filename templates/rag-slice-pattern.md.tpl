# Pattern — {{PATTERN_NAME}}

<!-- Slice: docs-private/rag/patterns/{{PATTERN_SLUG}}.md
     Word budget: ~500 words (upper bound; under is better).
     Scope: one code/test convention executors mirror. Includes canonical
     example + grep pattern. Self-contained — paste-ready. The executor
     reading this slice should be able to write code that matches the
     pattern without opening the canonical implementation. -->

## Source pins

- `{{CANONICAL_FILE}}:{{CANONICAL_LINE}}` — canonical implementation.
- `{{SIBLING_EXAMPLE_FILE}}:{{SIBLING_LINE}}` (optional second example).
- `{{TEST_FILE}}:{{TEST_LINE}}` — test that exercises the pattern.

## Pattern

**Pattern:** {{ONE_SENTENCE_PATTERN_STATEMENT}}

**Canonical implementation:** `{{CANONICAL_FILE}}:{{CANONICAL_LINE}}`

**Shape:**

```{{LANGUAGE}}
{{CANONICAL_EXAMPLE_CODE_BLOCK}}
```

<!-- The code block is trimmed to ~20 lines. Show the pattern, not the
     surrounding glue. If the canonical example is longer, elide internal
     details with `# ...` and keep the structural bones visible. -->

**Mirror this by:**

- {{MIRROR_RULE_1}}
- {{MIRROR_RULE_2}}
- {{MIRROR_RULE_3}}
- {{MIRROR_RULE_4}}

**Grep to verify:**

```bash
{{GREP_COMMAND}}
```

Expect: {{GREP_EXPECTATION_ONE_LINE}}.

<!-- example:
**Pattern:** every provider registers its intent set via a module-level
`INTENTS` frozenset that the dispatcher imports at startup.

**Canonical implementation:** `engines/alphamelts/provider.py:14`

**Shape:**

```python
from dispatch.intents import Intent

INTENTS: frozenset[Intent] = frozenset({
    Intent.SILICATE_LIQUIDUS,
    Intent.SILICATE_EQUILIBRIUM,
})

def handle(intent: Intent, payload: Payload) -> Result:
    if intent not in INTENTS:
        raise UnsupportedIntent(intent, provider=__name__)
    # ... dispatch on intent
```

**Mirror this by:**

- Declaring `INTENTS` at module top, frozenset literal — not a list, not built at runtime.
- Importing the canonical `Intent` enum from `dispatch.intents`; never redefine locally.
- Raising `UnsupportedIntent(intent, provider=__name__)` when handle is called outside the declared set.
- Adding the new provider's `INTENTS` to `dispatch/authority.py` in the same commit.

**Grep to verify:**

```bash
rg "^INTENTS: frozenset\[Intent\] = frozenset" engines/
```

Expect: one match per provider module; count equals `ls engines/*/provider.py | wc -l`.
-->

## Anti-patterns (do NOT do these)

<!-- Optional but recommended: 2-3 mistaken variants executors might write
     by accident. Each one-line; explain why it's wrong. -->

- {{ANTI_PATTERN_1}} — {{WHY_WRONG_1}}.
- {{ANTI_PATTERN_2}} — {{WHY_WRONG_2}}.

## Self-check before reporting done

- [ ] Pattern statement is one sentence, greppable.
- [ ] Canonical implementation cited with file:line; line verified to point at the pattern.
- [ ] Code block is ~20 lines, structural bones visible, no unrelated glue.
- [ ] Mirror rules are imperative ("Declare X", "Import Y") — not descriptive.
- [ ] Grep command verified to run and return expected matches (you ARE permitted to
      run it via Bash per rag-slice-builder.md rule 4).
- [ ] Self-contained: an executor never has to open the canonical file.
- [ ] Word count under budget. If over, the pattern is too broad; consider splitting
      into two pattern slices.
- [ ] Voice matches AGENTS.md: terse, technical, file:line refs.
