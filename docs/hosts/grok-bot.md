# Grok Bot host notes

Grok Bot controllers often arm portable `listen` through the user's registered
Mac (`local-exec`). When that session drops, doorbells die even though workers
still write the journal. Delivery is not lost — the controller only loses
promptness until something else wakes it.

## Dual doorbell

Two independent planes, both required for promptness on this host:

1. **Inbox / truth** = the journal on the laptop. Mail bodies, task tables, and
   secrets never leave that store. `relay` / `advance` are how the controller
   reads. The operator is not the mailman.
2. **Wake** = how a host without a persistent stdout Monitor gets a turn.
   Portable `listen` (exit-as-wake) and the outbound webhook are *independent*
   alternative doorbells. Claude keeps `supervise` on Monitor; Grok Bot keeps
   both `listen` and this webhook.

Either doorbell may fire; both may fire. Duplicate wakes are OK — the
controller peeks the journal and stays quiet if nothing is pending. Missing a
wake is not OK.

**Deafness** is both `listen` unarmed *and* the webhook failing (or unconfigured).
Re-arm listen and fix the webhook URL. Do not ask the operator to paste mail
or to tell another session to check mail.

## Optional outbound wake webhook

An operator-configured HTTP POST nudges an external Grok Bot webhook routine
when a controller-addressed waking event becomes listen-visible (the existing
journal delivery projection — `Journal.mark_delivery_projected` — not a second
inbox).

This complements exit-as-wake `listen`. It does **not** replace `listen` when
local-exec is up, and it is not a Grok Bot-native mail transport. The journal
remains durable truth; the POST carries no message body.

A configured URL enqueues a journal `wake_webhook_outbox` row in the same
transaction as that projection. After commit, a sender POSTs due rows.
Failed POSTs use bounded backoff and never roll back the journal write.
The next listen-visible harvest (the same `mark_delivery_projected` path)
retries due rows — there is no parallel daemon, so the doorbells stay
independent. Listen is not blocked on HTTP. Unset URL means zero HTTP and
no enqueue (a later-configured host does not inherit historical wakes).
Doctor warns when this host is in use and the URL is missing, or when a
configured URL has undelivered outbox rows.

Supervise heartbeat, follow keepalive, and unchanged `kind=next` reminders
never enqueue. Those stay Monitor-local / listen-timeout.

### Config (env or local file — never checked in)

Either export:

```bash
export GOALFLIGHT_WAKE_WEBHOOK_URL='https://<routine-host>/...'
export GOALFLIGHT_WAKE_WEBHOOK_SECRET='<sender-key-from-routine-panel>'
# optional; default bearer
export GOALFLIGHT_WAKE_WEBHOOK_AUTH=bearer   # or x-webhook-key
```

or write `~/.goal-flight/wake-webhook.json` (gitignored home state):

```json
{
  "url": "https://<routine-host>/...",
  "secret": "<sender-key-from-routine-panel>",
  "auth": "bearer"
}
```

`GOALFLIGHT_WAKE_WEBHOOK_CONFIG` overrides the file path. Env overlays file
fields. Unset/empty `GOALFLIGHT_WAKE_WEBHOOK_URL` and no file URL means
**zero HTTP**.

Paste the URL and sender key from the Grok Bot routine panel. Goal Flight
does not implement the receiver.

### Request

`POST` `application/json`, `User-Agent: goal-flight-wake-webhook/1`.

Auth, when a secret is set:

| `auth` | Header |
| --- | --- |
| `bearer` (default) | `Authorization: Bearer <secret>` |
| `x-webhook-key` | `X-Webhook-Key: <secret>` |

Body (nudge only — no mail text, secrets, or task tables):

```json
{
  "kind": "mail",
  "controller_label": "alice",
  "project_root": "/path/to/project",
  "dispatch_id": "chunk-id",
  "event_type": "controller-notice"
}
```

`kind` is `mail` (controller-channel / addressee types), `complete`
(`result` / `blocked` terminal harvest), or `wake` (other waking types).
`dispatch_id` is omitted for the journal `attention` stream. Identity is
`origin_node` + `event_uuid` (the journal delivery key). A crash between a
successful POST and the delivered mark may double-wake; that is OK.

Timeout defaults to 2s (`GOALFLIGHT_WAKE_WEBHOOK_TIMEOUT_S`, clamped 0.1–15).
POST failure is logged to stderr and recorded on the outbox row; the journal
delivery write already committed.

Portable listen semantics stay in `protocols/controller-mail.md`.
