# Grok Bot host notes

Grok Bot controllers often arm portable `listen` through the user's registered
Mac (`local-exec`). When that session drops, doorbells die even though workers
still write the journal. Delivery is not lost — the controller only loses
promptness until something else wakes it.

## Optional outbound wake webhook

An operator-configured HTTP POST can nudge an external Grok Bot webhook
routine when a controller-addressed waking event becomes listen-visible
(the existing journal delivery projection, not a second inbox).

This complements exit-as-wake `listen`. It does **not** replace `listen` when
local-exec is up, and it is not a Grok Bot-native mail transport. The journal
remains durable truth; the POST carries no message body.

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
`dispatch_id` is omitted for the journal `attention` stream. One POST per
newly projected waking recipient row; an idempotent replay does not POST
again.

Timeout defaults to 2s (`GOALFLIGHT_WAKE_WEBHOOK_TIMEOUT_S`, clamped 0.1–15).
POST failure is logged to stderr and ignored; the journal write already
committed.

Portable listen semantics stay in `protocols/controller-mail.md`.
