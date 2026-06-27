# <project> — History (write-once)

> Append-only project history — one entry per compaction handoff / rotation.
> NEVER edit prior entries. APPEND only; do not read the whole file back. Under
> context pressure, append a STUB (date + HEAD + one line) and flesh it out on a
> later, unpressured turn. Living current state is `handoff.md`; this is the
> reviewable archive (replaces dated `handoff-<DATE>.md` files).

## <YYYY-MM-DD> · <HEAD>

- Shipped: <what landed since the last entry>
- Focus: <what this stretch was about>
- Next: <the handoff's next action at this point>
