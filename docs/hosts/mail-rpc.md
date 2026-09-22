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
A pinned daemon label cannot relay or drain a different controller.

## Config (journal host)

| Env | Role |
| --- | --- |
| `GOALFLIGHT_MAIL_RPC_TOKEN` | Required. At least 16 characters. Generate on the journal host. |
| `GOALFLIGHT_MAIL_RPC_BIND` | Optional. Default `127.0.0.1:8787`. |
| `GOALFLIGHT_MAIL_RPC_ALLOW_PUBLIC_BIND` | Set to `1` only to allow `0.0.0.0` or `::`. Otherwise those binds refuse to listen. A Tailscale address is an explicit bind, not a public wildcard. |
| `GOALFLIGHT_CONTROLLER_LABEL` | Pin the mailbox. When set, a request header or body naming another label is **403**. |
| `GOALFLIGHT_PROJECT_ROOT` | Optional default checkout on the journal host. |

Request header `X-Goalflight-Controller-Label` supplies the label when the
daemon env is unset. Production pins the env so the token cannot select
another controller's mail.

Template (names only, no secret): `configs/grok-bot/mail-rpc.env.example`.

## Provisioning

Do this on the **journal host** (the machine that already holds
`~/.goal-flight`), not on the VPS.

1. Generate a token and keep it out of git and out of chat:

   ```bash
   umask 077
   mkdir -p ~/.goal-flight
   printf 'GOALFLIGHT_MAIL_RPC_TOKEN=%s\n' "$(openssl rand -hex 32)" \
     >> ~/.goal-flight/mail-rpc.env
   ```

   Append the other daemon settings in that same file (`GOALFLIGHT_MAIL_RPC_BIND`,
   `GOALFLIGHT_CONTROLLER_LABEL`, `GOALFLIGHT_PROJECT_ROOT`). For Tailscale,
   bind that interface address, for example `100.x.y.z:8787`, not `0.0.0.0`.

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
   Daemon names: GOALFLIGHT_MAIL_RPC_TOKEN, GOALFLIGHT_MAIL_RPC_BIND,
   GOALFLIGHT_CONTROLLER_LABEL.
   Example file: configs/grok-bot/mail-rpc.env.example.
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
