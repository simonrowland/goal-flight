# Journal-host mail RPC

Grok Bot controllers that cannot Mac local-exec / `listen` still read and
post against the journal on the host that holds `~/.goal-flight`.
`scripts/goalflight_mail_rpc.py` is a thin bearer-authenticated HTTP window
onto `goalflight_messages.py` (`relay --new`, `relay --drain`, `post`). It
does not open a second store on the VPS.

Wake webhooks stay nudge-only. They carry no mail body. See
[hosts/grok-bot.md](grok-bot.md) (dual doorbell / outbound wake webhook).

## Endpoints

| Method | Path | Auth | Effect |
| --- | --- | --- | --- |
| `GET` | `/v1/health` | no | Liveness. No mail, no token echo. |
| `POST` | `/v1/relay` | bearer | Peek (`relay --new --json`). `drain=1` or `{"drain": true}` runs `relay --drain`. |
| `POST` | `/v1/post` | bearer | `post --json`. `advisory` and unaddressed controller mail are refused the same way the CLI refuses them. |

Default mode for `/v1/relay` is peek. Drain acknowledges that snapshot.
A pinned label cannot relay or drain a different controller. In multi-user
mode the pin is the label stored for that bearer, not a header the caller
picks.

## Config (journal host)

| Env | Role |
| --- | --- |
| `GOALFLIGHT_MAIL_RPC_TOKEN` | Single-controller mode. Required only when no users file is selected. At least 16 characters. Generate on the journal host. |
| `GOALFLIGHT_MAIL_RPC_USERS_FILE` | Multi-controller mode. Path to the users JSON. When this is set, that file is the only bearer source. |
| `GOALFLIGHT_MAIL_RPC_BIND` | Optional. Default `127.0.0.1:8787`. |
| `GOALFLIGHT_MAIL_RPC_ALLOW_PUBLIC_BIND` | Set to `1` only to allow `0.0.0.0` or `::`. Otherwise those binds refuse to listen. A Tailscale address is an explicit bind, not a public wildcard. |
| `GOALFLIGHT_CONTROLLER_LABEL` | Single-controller pin. When set, a request header or body naming another label is **403**. Ignored when a users file is selected. |
| `GOALFLIGHT_PROJECT_ROOT` | Optional default checkout on the journal host. |

Which bearers are loaded:

1. If `GOALFLIGHT_MAIL_RPC_USERS_FILE` is set, that file is authoritative.
   `GOALFLIGHT_MAIL_RPC_TOKEN` is not accepted and is not a second door.
   `GOALFLIGHT_CONTROLLER_LABEL` is not applied. Remove the legacy token from
   the daemon environment after you migrate so the process does not keep a
   spare secret.
2. If that variable is unset and `GOALFLIGHT_MAIL_RPC_TOKEN` is set, the
   daemon stays in single-token mode. A file at the default path is not read.
3. If the token is also unset and `~/.goal-flight/mail-rpc.users.json`
   exists, that default file is used.
4. Otherwise the process refuses to start.

Request header `X-Goalflight-Controller-Label` supplies the label only in
single-token mode when `GOALFLIGHT_CONTROLLER_LABEL` is unset. Production
either pins that env or, for several bots, uses the users file so a token
cannot select another controller's mail.

`/v1/health` stays unauthenticated and does not report how many users are
configured.

Templates (names only, no secret): `configs/grok-bot/mail-rpc.env.example`
and `configs/grok-bot/mail-rpc.users.json.example`.

## Several controllers, one daemon

One bind address serves every Grok bot. Each bot still has one
`MAIL_RPC_URL` and one `MAIL_RPC_TOKEN`. The daemon maps that token to one
mailbox. A second port per bot is not required.

Users file (mode `600`):

```json
{
  "users": [
    {
      "token": "<openssl rand -hex 32>",
      "controller_label": "goalflight-grokbot",
      "project_root": "/absolute/path/on/journal/host"
    },
    {
      "token": "<openssl rand -hex 32>",
      "controller_label": "second-controller"
    }
  ]
}
```

Rules the loader enforces:

- `token` is required, at least 16 characters, and unique. The process keeps
  a SHA-256 digest and compares digests with `hmac.compare_digest`. It does
  not log the token or the `Authorization` header.
- `controller_label` is required on every entry. It must match the same
  bounded pattern as `GOALFLIGHT_CONTROLLER_LABEL`. After the bearer matches,
  that label is the only mailbox the request may peek, drain, or post as.
  Omitting the request label is fine. A different header or body label is
  **403**.
- `project_root` is optional. When set, that entry is confined to that
  directory; a different request path is **403**. The same pin covers
  `controller_project_root` on `/v1/post`. When omitted, the entry uses
  `GOALFLIGHT_PROJECT_ROOT` if that is set, and is confined to it. When
  neither is set, the request may still pass `project_root`, as in
  single-token mode.
- Single-token mode does not confine `project_root` or
  `controller_project_root`. The request path still wins, then
  `GOALFLIGHT_PROJECT_ROOT`.
- Duplicate tokens, an empty users array, unknown keys, and a users file
  that is group- or world-accessible are startup errors. `chmod 600` the
  file. The error text does not include token values.

Point each bot at the same URL and its own token:

- `MAIL_RPC_URL` — `http://<journal-host-tailscale-or-loopback>:8787`
- `MAIL_RPC_TOKEN` — that user's token, stored as a Grok secret

Clients do not learn about other users.

## Provisioning

Do this on the **journal host** (the machine that already holds
`~/.goal-flight`), not on the VPS.

1. Create the daemon env on the journal host. Keep secrets out of git and
   out of chat. For Tailscale, bind that interface address, for example
   `100.x.y.z:8787`, not `0.0.0.0`.

   One controller:

   ```bash
   umask 077
   mkdir -p ~/.goal-flight
   printf 'GOALFLIGHT_MAIL_RPC_TOKEN=%s\n' "$(openssl rand -hex 32)" \
     >> ~/.goal-flight/mail-rpc.env
   ```

   Append `GOALFLIGHT_MAIL_RPC_BIND`, `GOALFLIGHT_CONTROLLER_LABEL`, and
   optionally `GOALFLIGHT_PROJECT_ROOT` in that same file.

   Several controllers (one daemon, one bind — a second port is not
   required). Do not also set `GOALFLIGHT_MAIL_RPC_TOKEN` or
   `GOALFLIGHT_CONTROLLER_LABEL`; the users file replaces both.

   ```bash
   umask 077
   mkdir -p ~/.goal-flight
   install -m 600 /dev/null ~/.goal-flight/mail-rpc.users.json
   # Edit the file in place. Generate a distinct token per user.
   # Do not echo token values into the shell history.
   printf 'GOALFLIGHT_MAIL_RPC_USERS_FILE=%s\n' "$HOME/.goal-flight/mail-rpc.users.json" \
     >> ~/.goal-flight/mail-rpc.env
   ```

   Append `GOALFLIGHT_MAIL_RPC_BIND` and, if a user omits `project_root`,
   `GOALFLIGHT_PROJECT_ROOT`. Restart the daemon after editing the users
   file (launchd `kickstart -k`, or the systemd unit). Each bot gets the
   same `MAIL_RPC_URL` and a different `MAIL_RPC_TOKEN`.

2. Run the daemon from the Goal Flight checkout (or the pinned skill root).

   Foreground:

   ```bash
   set -a
   # shellcheck disable=SC1090
   . ~/.goal-flight/mail-rpc.env
   set +a
   python3 scripts/goalflight_mail_rpc.py serve
   ```

   systemd sketch (paths are the journal host's):

   ```ini
   [Service]
   EnvironmentFile=%h/.goal-flight/mail-rpc.env
   WorkingDirectory=/absolute/path/to/goal-flight
   ExecStart=/usr/bin/python3 scripts/goalflight_mail_rpc.py serve
   Restart=on-failure
   ```

   launchd sketch: `ProgramArguments` is `python3`, the absolute path of
   `scripts/goalflight_mail_rpc.py`, and `serve`. Put the same env keys in
   `EnvironmentVariables` or a wrapper that sources `~/.goal-flight/mail-rpc.env`.
   `KeepAlive` true. Do not copy the token into a repo plist.

3. On the Grok Bot side, set secret-request env **names** only:

   - `MAIL_RPC_URL` — `http://<journal-host-tailscale-or-loopback>:8787`
   - `MAIL_RPC_TOKEN` — the same token, stored as a Grok secret

   The client also accepts `GOALFLIGHT_MAIL_RPC_URL` and
   `GOALFLIGHT_MAIL_RPC_TOKEN`.

4. Startup prompt (names and paths only — never paste secret values):

   ```text
   Mail lives on the journal host. Read and post with
   scripts/goalflight_mail_rpc.py (docs/hosts/mail-rpc.md).
   Env names: MAIL_RPC_URL, MAIL_RPC_TOKEN.
   Daemon names: GOALFLIGHT_MAIL_RPC_TOKEN, GOALFLIGHT_MAIL_RPC_USERS_FILE,
   GOALFLIGHT_MAIL_RPC_BIND, GOALFLIGHT_CONTROLLER_LABEL.
   Example files: configs/grok-bot/mail-rpc.env.example,
   configs/grok-bot/mail-rpc.users.json.example.
   Do not paste token values into chat. Wake webhooks are nudge-only.
   ```

## Client

Peek:

```bash
python3 scripts/goalflight_mail_rpc.py relay \
  --project-root /absolute/path/on/journal/host
```

Drain:

```bash
python3 scripts/goalflight_mail_rpc.py relay --drain \
  --project-root /absolute/path/on/journal/host
```

Post (addressed `controller-notice`, not `advisory`):

```bash
python3 scripts/goalflight_mail_rpc.py post \
  --dispatch-id chunk-id \
  --type controller-notice \
  --to-controller goalflight-grokbot \
  --project-root /absolute/path/on/journal/host \
  --text 'hello'
```

curl:

```bash
curl -sS "$MAIL_RPC_URL/v1/health"

curl -sS -X POST "$MAIL_RPC_URL/v1/relay" \
  -H "Authorization: Bearer $MAIL_RPC_TOKEN" \
  -H "X-Goalflight-Controller-Label: $GOALFLIGHT_CONTROLLER_LABEL" \
  -H 'Content-Type: application/json' \
  -d '{"project_root":"/absolute/path/on/journal/host"}'

curl -sS -X POST "$MAIL_RPC_URL/v1/post" \
  -H "Authorization: Bearer $MAIL_RPC_TOKEN" \
  -H "X-Goalflight-Controller-Label: $GOALFLIGHT_CONTROLLER_LABEL" \
  -H 'Content-Type: application/json' \
  -d '{"dispatch_id":"chunk-id","type":"controller-notice","to_controller":"goalflight-grokbot","project_root":"/absolute/path/on/journal/host","text":"hello"}'
```

`project_root` is a path on the journal host. The RPC rejects `--type advisory`
and controller mail with no `--to-controller`, and returns the CLI's refusal
text.

## Run locally

```bash
export GOALFLIGHT_MAIL_RPC_TOKEN="$(openssl rand -hex 32)"
export GOALFLIGHT_CONTROLLER_LABEL=goalflight-grokbot
export GOALFLIGHT_PROJECT_ROOT="$PWD"
python3 scripts/goalflight_mail_rpc.py serve
```

Another shell, same token in `GOALFLIGHT_MAIL_RPC_TOKEN` or `MAIL_RPC_TOKEN`:

```bash
export GOALFLIGHT_MAIL_RPC_URL=http://127.0.0.1:8787
curl -sS "$GOALFLIGHT_MAIL_RPC_URL/v1/health"
python3 scripts/goalflight_mail_rpc.py relay --project-root "$PWD"
```
