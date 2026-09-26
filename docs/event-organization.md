# Event organization

**Status: architecture review, 2026-09-23.** This is a backlog for the
goal-flight controller. It does not change runtime behavior. Measured
history stays in [EVENT-ARCHITECTURE.md](EVENT-ARCHITECTURE.md). Host
procedures stay in [hosts/grok-bot.md](hosts/grok-bot.md) and
[hosts/mail-rpc.md](hosts/mail-rpc.md).

Draft PR #16 (multi-token mail-RPC users file) is the intended mail-plane
direction. It is not merged. This review treats that shape as the target
and the single-token daemon on `main` as what runs today.

The useful split is three planes plus hops. One config map per controller
label is the right end state for Mac-less multi-controller hosting. The
tree does not have that map yet, and it should not be invented by merging
today's files in one cut.

---

## Verdict

| Plane | Question | Authority today | Scope |
| --- | --- | --- | --- |
| **Journal** | What happened, and who has consumed it? | Per-project SQLite under `~/.local/state/goal-flight/journals/<project>/` | Project |
| **Mail** | What does the body say? | `~/.goal-flight/messages/*.jsonl`, rendered by `goalflight_messages.py relay` | Fleet (one home directory) |
| **Wake** | How soon does a controller get a turn? | Adapters below. None of them are a second inbox. | Per controller, per project |

Hops are how a process reaches a plane. They are not extra stores.

| Hop | What it is |
| --- | --- |
| `goalflight_mail_rpc.py` | Bearer-authenticated HTTP onto `relay` / `post` on the journal host. Default bind `127.0.0.1:8787`. |
| `goalflight_grok_bot_listen.py` | Host wrapper around `listen`. Adds the quote-check banner and defaults (`--timeout-s 900`, `--report-pending`, label `goalflight-grokbot`). |
| `goalflight_mcp_messages.py` | MCP ingress that posts the same `goalflight.message.v1` envelopes. |
| `supervise` | Wake adapter that spawns the follow stream, backup `listen` doorbells, and a watchdog, then multiplexes their stdout. |

`goalflight_steer_mailbox.py` is a worker question channel inside a
dispatch. `com.goalflight.drain` (`protocols/drainer.md`) launches queued
dispatch envelopes. `codex-seatd` / `grok-seatd` refresh account seats.
Leave those three outside the event planes.

The 2026-08-21 decision in EVENT-ARCHITECTURE §8 — move delivery and wake
rows out of the project journal and next to `~/.goal-flight/messages/` —
**has not shipped.** `delivery_events`, `controller_cursors`,
`listener_coverage`, and `wake_webhook_outbox` are still journal tables
(`scripts/goalflight_journal.py`). File tasks against the schema that is
in the tree.

---

## 1. Event taxonomy

Names overlap. The structures do not.

### Journal rows (durable)

| Record | Types that matter |
| --- | --- |
| `dispatch_attempts` / `dispatch_transitions` | Lifecycle `PREPARED` `STARTING` `RUNNING` `TERMINAL` `ABANDONED`. |
| `terminal_outbox` | `result`, `blocked`, `user_need`, `user_confirm`. |
| `delivery_events` | Any registered message type. `wake_class` is `waking` or `quiet`. Identity is `(project_root, recipient_label, origin_node, event_uuid)`. |
| `controller_leases` | `ACTIVE` `SUPERSEDED` `EXPIRED` `RETIRED`. Label + generation is the fence. Nonce is recorded per generation. |
| `controller_cursors` / stream cursors | Mail read position. `advanced_by` is the consumer. |
| `listener_coverage` | Audit of an armed `listen`. Kernel slot locks are liveness; this table is not. |
| `attention_items` / `system_attention_items` | Open work. `wake_class` is fixed to `waking`. |
| `wake_webhook_outbox` | One nudge row per delivery key. HTTP outcome lives here. |

### Mail envelopes

`EVENT_TYPE_REGISTRY` in `scripts/goalflight_messages.py` is the ingress
contract. Canonical types include `status`, `monitor`, `user_need`,
`user_confirm`, `result`, `blocked`, `advisory`, `steering`, and the
controller channel (`controller-question`, `controller-answer`,
`controller-notice`, `controller-coordination`, plus legacy `coordination`
and `notice`).

A long compatibility map collapses measured historical spellings onto
those canonical lifecycle contracts. `finding`, `patch`, and
`merge-request` inherit `advisory`'s lifecycle and remain distinct
addressee types (`CONTROLLER_ADDRESSEE_TYPES`). `advisory` itself cannot
carry a controller addressee at the CLI. Do not "simplify" that map in a
drive-by: it is the D13 compatibility vocabulary, and unregistered types
fail closed.

`wake_class` on the envelope registration is what makes a delivery
listen-visible. A `user_need` whose payload `nudge_kind` is
`parallel-ready`, `resume-ready`, or `done-suggest` is forced to `quiet`.

### Wake adapters (promptness only)

| Adapter | Signal | Host |
| --- | --- | --- |
| `listen` / `listen-auto` | Process exit. `0` ring, `1` timeout, `2` journal unreadable, `3` contention, `4` detached refusal, `5` dead nonce. | Any host that surfaces a tracked task's exit. Grok Bot uses this. |
| `follow` | JSON lines on stdout: `kind=event`, `kind=heartbeat`, `kind=frontier`. Also structural `kind=event` faults. | Hosts with a persistent stdout monitor. |
| `supervise` | Owns stream + backup doorbells + watchdog. Stdout adds `kind=supervise` records (`arm`, `restart`, `stop`, `next`, coverage loss). | Claude Code Monitor. Default cadence: stream heartbeat 120s (legal 60–300), supervisor peer probe 3600s, backup depth 2. |
| Wake webhook | HTTP POST after `mark_delivery_projected` commits. Body `kind` is `mail`, `complete`, or `wake`. | Grok Bot routine. Nudge only: label, project, dispatch id, event type. No body, no secret. |

Grok's 900-second `listen --timeout-s` is a frontier **ping** (exit 1).
It is not a `follow` heartbeat and not a `kind=frontier` line. Docs that
call it a heartbeat are using the word loosely.

### Webhook `kind` vs message `type`

`classify_nudge_kind` maps `result`/`blocked` to `complete`, the
controller-channel set plus `advisory`/`finding`/`patch`/`merge-request`
to `mail`, and everything else to `wake`. Supervise heartbeat, coverage,
frontier, and `kind=next` never enqueue. The word `kind` on a webhook
body is this three-way nudge class. The word `kind` on a follow line is
`event` / `heartbeat` / `frontier`. The word `kind` on a supervise line
is `supervise`.

### Words that name two mechanisms

| Word | Use the precise one |
| --- | --- |
| `drain` | `relay --drain` receipts mail. `com.goalflight.drain` launches queued dispatches. |
| `wake` | The promptness plane, the `wake_class` column, the webhook nudge class `wake`, the roster field `wake_armed`, and `goalflight_wake.py` (slot locks). |
| `event` | A `delivery_events` row, or a follow stdout line with `kind=event`. |
| `listen` | The doorbell command. "Listen-visible" means the delivery row has been projected. It does not mean a `listen` process is running. |
| `doorbell` | `listen`'s exit, and the webhook POST. They are independent adapters of one plane. |
| `heartbeat` | Follow's 120s line, supervise's 3600s silent peer probe, or (loosely) Grok's 900s timeout. |
| `frontier` | A follow `kind=frontier` line, a supervise `kind=next` reminder, or Grok's timeout ping. |
| `coverage` | `listener_coverage` (audit), kernel slot coverage, or supervise `live/4`. |
| `seat` | Account rotator (`codex-seatd`, `grok-seatd`). Not a controller label. |

---

## 2. Planes vs hops

The planes are separated in storage and mostly blurred at the edges where
a host adapter has to wake and show mail in one turn.

Separated:

- Webhook module docstring and `docs/hosts/mail-rpc.md`: HTTP nudge carries
  no mail body. Mail-RPC does not serve the webhook.
- `follow` heartbeats and unchanged frontiers do not enqueue webhook rows.
- Envelope bytes live in `~/.goal-flight/messages/`. Consumption position
  lives in the journal cursor. `relay` is peek; `relay --drain` is the
  explicit receipt.
- A detached `listen` exits 4 because its exit would wake nobody.

Blurred, on purpose, and worth keeping visible:

- `supervise` is a wake multiplexer that starts `listen` children. The
  persistent path contains the portable path.
- `listen --report-pending` prints mail headlines on the way out. The wake
  process renders payload. That is acceptable for exit-as-wake. It is a
  poor place to add a second store.
- Webhook enqueue and `flush_due` run inside journal projection
  (`mark_delivery_projected` → `_flush_wake_webhook_after_projection`).
  The wake sender is on the durable write path. Retry of a failed POST
  happens on the **next projection**, not on a clock and not on
  `relay`. Mail-RPC reads do not flush the outbox.
- Grok "dual doorbell" (`docs/hosts/grok-bot.md`) is two wake adapters
  (`listen` and the webhook) sharing one journal. Mail-RPC is how the
  controller reads after either adapter fires. It is not a third doorbell.

Mac-less path that matches the tree:

1. A projection on the journal host enqueues `wake_webhook_outbox` and
   POSTs a nudge to the single configured URL.
2. Grok, woken by that routine, calls mail-RPC `relay` / `post` against
   the same journal.
3. `listen` on the Mac is the other doorbell when local-exec is up.
   Missing both adapters loses promptness. The journal still has the
   event.

---

## 3. Identity and tenancy

These are different credentials. A future map may point at all of them.
They must not be collapsed into one secret.

| Identity | What it authorizes | Where it lives |
| --- | --- | --- |
| Controller **label** | Mailbox name. Primary key for cursors, leases, webhook recipient. | Lease row, envelope addressee, RPC pin, webhook payload. |
| Lease **generation** + nonce + pid + start token | Who may dispatch as that label, and which listeners are current. Kernel lock is liveness. | `controller_leases` plus wake-ledger slot locks under `~/.local/state/goal-flight/wake-ledger/`. |
| Session id | The nonce carried as `GOALFLIGHT_CONTROLLER_SESSION_ID` / `--controller-session-id`. | Process env. Not a second label. |
| Author digest | Attribution on a post. The label is descriptive metadata; the digest is the capability stamp. | Envelope. |
| `UNKNOWN` | Explicit "sender not establishable". Never a guessed label. | Envelope `source.controller_label`. |
| **Project root** | Which journal, task store, and wake ledger. Canonical key is the worktree-stripped repo path, slug plus hash (`goalflight_task.resolve_task_store_dir`). | Request `project_root` on mail-RPC. CWD on the CLI. |
| Mail-RPC **bearer** | Right to call `relay` / `post` as a label. | Today: `GOALFLIGHT_MAIL_RPC_TOKEN` in the daemon env, optionally pinned by `GOALFLIGHT_CONTROLLER_LABEL`. PR #16: users file, one digest per entry, each entry pinned to one label and an optional project root. |
| Webhook **secret** | Right for the journal host to POST a nudge. | `~/.goal-flight/wake-webhook.json` or `GOALFLIGHT_WAKE_WEBHOOK_*`. One URL, one secret, whole host. |
| Delivery key | `origin_node` + `event_uuid` inside a project and recipient. | Journal. Idempotency for projection and for at-least-once nudges. |
| Dispatch / attempt id | A piece of work. | Journal. Not a controller. |
| Fleet node | SSH dispatch target. | `~/.goal-flight/fleet`. |
| Seat | Which vendor account a worker uses. | `com.goalflight.*-seatd`. |

### What multi-controller hosting should look like

**Mail.** One daemon, one bind, many bearers. That is PR #16:
`GOALFLIGHT_MAIL_RPC_USERS_FILE`, default path
`~/.goal-flight/mail-rpc.users.json` when no legacy token is set. When
the env path is set, the file is the only bearer source and
`GOALFLIGHT_CONTROLLER_LABEL` is not applied. Legacy single-token mode
stays for a daemon that still has `GOALFLIGHT_MAIL_RPC_TOKEN`. Parallel
daemons on `:8787` and `:8788` are the interim until that file is what
production runs. Keep the interim until the cutover; do not make ports
the long-term tenant key.

PR #16 rejects duplicate token digests, a loose file mode, an empty
users array, and unknown keys. It compares digests with
`hmac.compare_digest`. The example file still holds the token string on
disk (mode 600). Confirm, when filing the follow-up, whether two entries
may share one `controller_label`. Two bearers that can both `relay
--drain` the same mailbox are a drain race unless one of them is a
rotation window with the old token removed.

**Wake.** One URL in `wake-webhook.json` is a host-global route.
`flush_due` loads that single config and POSTs every due row, for every
`recipient_label`, to it. The JSON body includes `controller_label`, so
a receiver can ignore someone else's nudge. The sender cannot aim a
second URL. Two Mac-less controllers on one journal host therefore
share a doorbell. That is the coexistence gap. Claude `supervise` does
not use this file; a wake route should be optional per label so a
Claude controller does not grow an HTTP doorbell by accident.

**Lease.** Mail-RPC does not claim a lease. A bearer pinned to
`goalflight-grokbot` can peek and post as that label while the Mac
lease is held by another process, or while no lease exists. That is
correct for mail and dangerous for dispatch: terminal projection in
`Journal` assigns an attempt with an active owner to that owner, and
assigns an attempt whose owner is missing to **every active lease
label** in the project (wildcard `*` only when the roster is empty).
A Mac-less orchestrator that launches work without a live lease fans
terminal wakes across the project. Stamp a real lease, or pass the
explicit unowned hatch and expect the fanout.

### Config end state (do not cut over in one step)

```text
~/.goal-flight/
  mail-rpc.env                 # legacy single-token daemon; keep until users-file cutover
  mail-rpc.users.json          # PR #16; mode 600; token strings; not in git
  wake-webhook.json            # today's single URL; remains the default route
  wake-webhook.routes.json     # later: [{controller_label, url, auth}] ; absent = current behavior
  messages/                    # envelopes; leave
  skill/                       # pin; leave
  fleet/                       # leave
```

Bind the three by **label** in documentation first. A single
`controllers.json` that embeds tokens and webhook secrets is a later
convenience, after both files exist and production has moved. Building
it now creates a third config beside env files the live daemons already
source.

---

## 4. Source of truth when Grok orchestrates

The Mac (journal host) holds the stores. The VPS holds a wake routine
and a mail-RPC client. A floating Tailscale address for the VPS is fine.
Copying SQLite or JSONL onto the VPS is a second store; do not.

| Fact | Authority | How Grok may read it | Failure if misread |
| --- | --- | --- | --- |
| Dispatch outcome, delivery, cursor, lease rows | Project journal on the journal host | Mail-RPC `relay` / `post` only. RPC has no task, roster, or lease API. | A VPS sqlite diverges immediately. |
| Envelope body | `~/.goal-flight/messages/` on the journal host | Same RPC, which shells `goalflight_messages.py`. | Same. |
| Who is the live controller | Lease row + kernel lock on the machine that claimed | `goalflight_session_status.py` on that machine. | Roster `wake_armed` describes Mac listeners / supervise, not "the webhook routine is healthy". |
| Tasks and frontier | `task-stores/<slug>/` and `docs-private/tasks.jsonl` on the checkout the store was keyed to | Not on the mail-RPC surface. `follow` reads the materialized `tasks-data.js` and tags it `stale` or `projected`. | Grok guessing `next` from chat skips the store. |
| Resume pin | Newest `docs-private/RESUME-NOTES-*.md` in the checkout | Write it on the checkout Grok can actually edit (Mac shell / local-exec). | Notes that exist only in the VPS chat are invisible to the next Mac-side resume. |
| Wake promptness | Webhook outbox (journal) and listen slot locks (wake ledger on the host that armed them) | Webhook fires when a projection commits. Listen slots exist only if something on the journal host armed `listen`. | First POST failure waits for another projection. No listen and a dead URL means deaf, with mail intact. |
| Capacity, dispatch status files | `GOALFLIGHT_STATE_DIR`, defaulting to the short-lived state dir (`goalflight_compat.resolve_state_dir`) | On the dispatch host. | `/tmp` status is not journal truth. |

Invariants to keep in every task from this review:

- The journal is what happened.
- A missed wake costs latency.
- Do not drain another label. A pinned daemon (env label today, users-file
  entry in #16) is what enforces that. An unpinned daemon accepts
  `X-Goalflight-Controller-Label` from the client.
- Do not displace a live lease.
- Webhook bodies stay nudge-only.

---

## 5. Packaging surface

Live controllers load the pin `~/.goal-flight/skill/` (see
[skill-pin.md](skill-pin.md)), not this checkout, except while
`goalflight_skill_link.py --live` is flipped. A docs change here does
nothing for a running controller until the pin moves or the session
uses the live tree.

| Surface | Role |
| --- | --- |
| `SKILL.md` + `commands/` + `protocols/controller-mail.md` | Claude (and every host that loads the core skill). Wake instruction is `supervise`. |
| `configs/grok-bot/skills/goal-flight/SKILL.md` | The only file `./install.sh grok-bot` recopies into the workflows library. Core scripts move with the pin, not with that install. |
| `docs/hosts/grok-bot.md` | Dual doorbell, 900s listen, mail-RPC pointer. |
| `docs/hosts/mail-rpc.md` | Daemon env, endpoints, Tailscale bind. |
| `docs/hosts/{cursor,opencode,linux,windows}.md` | Install and sandbox. They do not describe wake. |
| `scripts/goalflight_messages.py` (~9800 lines) | Post, relay, listen, follow, supervise CLI. |
| `scripts/goalflight_wake.py` (~4000) | Slot locks, coverage, re-arm command strings. |
| `scripts/goalflight_wake_supervise.py` (~2800) | Supervisor loop. |
| `scripts/goalflight_journal.py` (~6500) | Schema and projection. |
| `scripts/goalflight_mail_rpc.py` | HTTP hop. |
| `scripts/goalflight_wake_webhook.py` | Nudge outbox client. |
| `scripts/goalflight_grok_bot_listen.py` | 70-line host wrapper. |

There is no `docs/hosts/claude.md`. Claude's wake contract is `SKILL.md`
plus EVENT-ARCHITECTURE §9. That is fine until someone "ports" supervise
defaults into the Grok wrapper. Grok must keep exit-as-wake; the 120s
stream heartbeat would spam turns. The wrapper already says so.

Script paths are the public API for launchd, systemd, and host shells.
`python3 scripts/goalflight_messages.py listen` and
`python3 scripts/goalflight_mail_rpc.py serve` stay where they are.
Internal splits belong behind those filenames.

---

## 6. Reorganization proposals

### Boundaries worth drawing

Keep three import surfaces, even while the files stay large:

1. **Journal** — `goalflight_journal.py`. Schema, leases, cursors,
   delivery, outbox rows. No HTTP.
2. **Mail** — envelope registry, post, relay, drain, MCP. The registry
   stays the single type list. Webhook classification should call it
   (or a named subset of it) instead of keeping a second frozenset in
   `goalflight_wake_webhook.py` that can drift from
   `CONTROLLER_ADDRESSEE_TYPES`.
3. **Wake** — `goalflight_wake.py`, `goalflight_wake_supervise.py`,
   `goalflight_wake_webhook.py`, `goalflight_grok_bot_listen.py`.
   Slot locks, stdout monitor, HTTP nudge, host wrappers.

`goalflight_mail_rpc.py` stays a thin hop. It should not grow journal
SQL or webhook sends.

### Naming

- Say **nudge** for the webhook body and **doorbell** only for `listen`'s
  exit, or call both "wake adapters" and name which.
- Say **receipt** for `relay --drain` and **queue launch** for
  `com.goalflight.drain`.
- Say **frontier ping** for Grok's exit 1. Reserve **heartbeat** for
  follow's tagged line.
- Call mail-RPC a **hop**. Call the users file the **mail tenant map**.
  Call per-label URLs the **wake route table**.

### Staged migration

Live labels and checkouts (battery-*, goal-flight, gf-webhook,
pm2-control, regolith-empirical, and any other pinned
`GOALFLIGHT_CONTROLLER_LABEL`) keep working through every stage.
No stage renames env vars, deletes `wake-webhook.json`, or moves
`scripts/goalflight_*.py`.

| Stage | Change | Ships when |
| --- | --- | --- |
| 0 | This document. | Now. |
| 1 | Land PR #16 as written: users file **or** legacy token, never a mix that ignores the pin. Cut production from a pair of port-pinned daemons to one bind only after each label has been peeked on the new daemon. Leave `:8788` up until that peek succeeds. | #16 review. |
| 2 | Additive `wake-webhook.routes.json`. Lookup is `recipient_label`. No matching route falls through to today's single URL. Absent routes file means today's behavior for every label, including Claude projections that happen to be waking. A route with `"disabled": true` suppresses the nudge for that label (Claude, or a controller that only uses `listen`). | After two Mac-less controllers need different routines. |
| 3 | Journal-host flush timer: a short-lived process that only calls `flush_due` on journals with pending outbox rows. Still nudge-only. Still no second store. Listen and relay stay correct without it; the timer covers "first POST failed and nothing new was projected". | After a measured stuck outbox, or before relying on Mac-less wake without `listen`. |
| 4 | Point webhook `kind=mail` classification at the registry subsets so `advisory` aliases cannot drift. | Small, independent of tenancy. |
| 5 | Split `goalflight_messages.py` internally (relay/post vs listen/follow) **behind the same CLI**. Re-run the message and listen tests. Do not change argv. | Only when a change is already going to touch both halves. |
| 6 | Optional `controllers.json` that references the users entry and the wake route by label. Do not embed a second copy of the token. | After stages 1 and 2 are what production runs. |

Stage 8 of EVENT-ARCHITECTURE (fleet-scoped delivery sqlite beside the
messages directory) stays **unscheduled**. It is a real consistency fix
for cross-project mail and worktree journals, and it is a data migration
across every project journal. Mac-less hosting does not require it:
mail-RPC already takes `project_root` and opens that project's journal.
Do it as its own design, with a dual-read period, not as part of the
webhook route table.

---

## 7. Backlog

### P0 — correctness before a third controller shares the host

1. **Confirm production pins.** Each interim mail-RPC daemon
   (`:8787`, `:8788`) has `GOALFLIGHT_CONTROLLER_LABEL` set, and the
   tokens differ. An unpinned daemon honors the client header and can
   drain any label. Check env **names** and bind addresses; do not paste
   token values into the task or the chat.
2. **Lease or explicit fanout.** Any Mac-less dispatcher that launches
   workers must hold that label's lease (pid + start token + nonce on
   the journal host) or use the documented unowned hatch knowing
   terminals fan out to every active label. Do not infer a label from
   the git directory name.
3. **Treat the single wake URL as shared.** Until stage 2, every
   waking projection for every label on that host POSTs to one routine.
   The receiver must ignore `controller_label` values it does not own,
   or a second controller's mail wakes the first bot. Payload stays
   nudge-only.
4. **Do not implement EVENT-ARCHITECTURE §8 as if it were current.**
   Delivery rows are still in the project journal.

### P1 — tenancy (multi-controller wake + mail)

5. Land PR #16. Keep legacy token mode. Cut over one label at a time.
6. Decide duplicate-label policy in the users file (reject a second
   entry, or allow it only as an overlapping rotation). Default
   recommendation: reject duplicate labels so two bearers cannot both
   drain one mailbox. Rotation means replace the token in the entry.
7. Add the wake route table (stage 2) with default-URL fallback.
8. Document, in `docs/hosts/grok-bot.md`, that mail-RPC identity is not
   a lease and that `wake_armed` does not report webhook health.
9. Add a doctor hint when more than one active lease label exists and
   the wake config is a single URL. Hint text only; no secret echo.
   Doctor already warns when the URL is missing or the outbox is
   undelivered (`check_wake_webhook`).

### P2 — sprawl, after the tenancy files exist

10. Classify webhook nudges from the registry subsets (stage 4).
11. Flush timer for the outbox (stage 3), journal host only.
12. Split the messages CLI internally without moving the script path
    (stage 5).
13. One paragraph in a future `docs/hosts/claude.md` (or a heading in
    `docs/architecture.md`) stating that Claude's wake adapter is
    `supervise` and that `wake-webhook.json` is a Grok adapter. Stops
    the next port from copying the wrong cadence.
14. Index this file from the skill only by path, not by pasting the
    backlog into `SKILL.md`. `SKILL.md` is the always-loaded surface.

### P3 — later

15. A label-keyed `controllers.json` that references the users entry
    and the wake route (stage 6).
16. Fleet-scoped delivery state (EVENT-ARCHITECTURE §8), as its own
    migration, if cross-project mail is still landing in the sender's
    journal.
17. Trim the compatibility alias list once a measured window shows the
    old spellings are unread. Not before.
18. Rename nothing the launchd labels already use (`com.goalflight.drain`,
    seatd logs).

---

## 8. Non-goals

- Do not merge PR #16 in this review, and do not rewrite it into a
  general event bus.
- Do not put mail bodies, cursor state, or tokens in webhook JSON.
- Do not add a mail store on the VPS.
- Do not replace `listen` or `supervise`. Grok stays exit-as-wake.
  Claude stays the persistent monitor. Both stay correct because the
  journal is shared.
- Do not move `delivery_events` in the same change as the users file
  or the wake routes.
- Do not fold seatd, the dispatch drainer, or the steer mailbox into
  the wake plane.
- Do not change heartbeat intervals, listener slot defaults, or the
  Grok 900s timeout as a "cleanup".
- Do not rename `scripts/goalflight_messages.py`,
  `scripts/goalflight_mail_rpc.py`, or the `GOALFLIGHT_*` env vars
  live daemons already source.
- Do not steal a live lease or drain a label this controller does not
  own, including while testing the route table.
- Do not paste tokens, webhook secrets, or Tailscale addresses into
  tasks, commits, or docs.

---

## 9. Controller digest

File these as tasks. No secrets.

1. P0: On the journal host, confirm each mail-RPC daemon is pinned with `GOALFLIGHT_CONTROLLER_LABEL` and its own token (ports 8787 and 8788 until the users file is live). Unpinned mode honors the client label header.
2. P0: Mac-less dispatch must hold the label's lease on the journal host, or accept terminal fanout to every active label. Mail-RPC does not claim a lease.
3. P0: Until a per-label wake route exists, one `~/.goal-flight/wake-webhook.json` URL receives every controller's nudge. Receiver filters on `controller_label`. Bodies stay label, project, dispatch id, event type.
4. P0: Do not move `delivery_events` or `wake_webhook_outbox` out of the project journal. EVENT-ARCHITECTURE §8 is not shipped.
5. P1: Land draft PR #16 (one daemon, users file, token digest pin per label, optional project root). Keep single-token mode when `GOALFLIGHT_MAIL_RPC_TOKEN` is set and the users-file env is unset.
6. P1: In that users file, reject two entries with the same `controller_label` unless a written exception says otherwise. Token rotation replaces the entry.
7. P1: Add optional `wake-webhook.routes.json` keyed by `controller_label`, falling back to the existing single URL when the file or the label is absent. `disabled` skips the nudge for that label.
8. P1: Docs: mail-RPC is a hop; `wake_armed` reports listen/supervise coverage, not webhook health.
9. P1: Doctor hint when several active labels share one wake URL.
10. P2: Drive webhook nudge class `mail` from `EVENT_TYPE_REGISTRY` / addressee sets, not a second hand-maintained frozenset.
11. P2: A journal-host timer that only runs wake-webhook `flush_due`. No new store. Covers a failed POST when no later projection occurs.
12. P2: Internal split of `goalflight_messages.py` (mail commands vs listen/follow) with the same argv and path.
13. P2: A short Claude host note: wake adapter is `supervise`; do not copy it onto Grok; do not point `wake-webhook.json` at Claude.
14. P3: Defer fleet-scoped delivery sqlite (EVENT-ARCHITECTURE §8) to its own design. Mac-less mail already passes `project_root`.
15. P3: Defer a combined `controllers.json` until the users file and the wake route table are both what production runs. Reference them by label; do not duplicate tokens.
16. Non-goal: no second inbox on the VPS, no mail bodies in webhooks, no lease theft, no script-path renames, no cadence changes, seatd and `com.goalflight.drain` stay out of this plane.
