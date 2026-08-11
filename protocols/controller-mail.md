# Controller mail

One shared inbox, not a private markdown drop.

Do NOT invent a per-pair notes file (`NOTES-to-pm2.md`, `handoff-kiln.md`). A
peer cannot see it, nothing records whether it was read, and the next controller
in that project has no way to discover it. Everything below goes through
`scripts/goalflight_messages.py` so the message is addressable, has a read
cursor, and survives your session ending.

## Read

```bash
# one FROM/subject headline per unseen message
python3 <skill-root>/scripts/goalflight_messages.py relay --new
# ... and advance the read cursor through what was shown
python3 <skill-root>/scripts/goalflight_messages.py relay --new --ack
# full envelopes instead of headlines
python3 <skill-root>/scripts/goalflight_messages.py relay --new --bodies
# one dispatch's thread -- the last N envelopes, only what is unseen, as JSON
python3 <skill-root>/scripts/goalflight_messages.py read --dispatch-id <id> --last 4
python3 <skill-root>/scripts/goalflight_messages.py read --dispatch-id <id> --unseen --ack
python3 <skill-root>/scripts/goalflight_messages.py read --dispatch-id <id> --last 1 --json
```

Mail addressed to another project is hidden by default; add `--all-projects`.

Fetch bodies deliberately. `--new` headlines are the scan; pulling every body on
every check is how a controller burns its context on correspondence.

**Do not hand-parse the JSONL.** A controller was seen running
`python3 -c "import json; lines=[json.loads(l) for l in open(...)]"` to find the
last seq and recent senders on one thread. `read --dispatch-id <id> --last 4`
returns exactly that. If you are reaching for a file path under
`~/.goal-flight/messages/`, the command you want already exists -- check
`read --help` and `relay --help` first. Hand-parsing skips the cursor, the
schema validation, and the corruption reporting.

## Who can I write to

```bash
python3 <skill-root>/scripts/goalflight_session_status.py \
  --list-controllers --project-root "$PWD"
```

Each row is a durable registry label plus its liveness. Register or adopt your
own so peers have something to address:

```bash
python3 <skill-root>/scripts/goalflight_session_status.py --register <name> --project-root "$PWD"
python3 <skill-root>/scripts/goalflight_session_status.py --join <name>     --project-root "$PWD"
```

`--register` mints a new identity; `--join` adopts an existing one whose owner
is gone (read its mail first, then continue or retire it).

## Send

```bash
python3 <skill-root>/scripts/goalflight_messages.py post \
  --dispatch-id <topic-slug> --type user_need \
  --to-controller <label> --subject '<one scannable line>' \
  --text '<body>'
```

- `--to-controller` addresses a registry label. `--controller-project-root`
  scopes it and defaults to the current git project; set it explicitly only for
  cross-project mail.
- **Always set `--subject`.** Without it the peer's relay listing shows your
  first body line, which is how a blocking request reads as noise.
- Types: `user_need` is tracked open until acknowledged — use it when you are
  blocked on the peer. `status` is informational. `result` closes a handoff.
  Answer an escalation with `ack`.

## Streaming, not polling

Arm one listener per claimed controller so mail wakes you instead of being
noticed whenever you happen to look:

```bash
python3 <skill-root>/scripts/goalflight_messages.py listen --project-root "$PWD"
```

Run it in the background. It blocks silently until mail arrives, prints it, and
exits — re-arm after each wake. A controller with no listener is polling, and
will discover a blocking `user_need` only on its next manual check.

## Backlog

Never delete correspondence to clear a backlog — a deleted escalation still
happened, and the sender is still waiting.

```bash
# digest the machine-wide unread snapshot without destroying it
python3 <skill-root>/scripts/goalflight_messages.py triage-backlog
# named mail whose recipient is not registered anywhere
python3 <skill-root>/scripts/goalflight_messages.py undeliverable
```

`undeliverable` is the tell that a peer retired a label, or that a sender
guessed one. Re-address rather than dropping.
