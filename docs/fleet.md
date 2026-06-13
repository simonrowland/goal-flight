# Multi-node fleet operations

Goal Flight 1.0 adds a **fleet layer** for dispatching ACP workers on remote
machines over SSH while the orchestrator stays on your laptop or CI runner. The
fleet store tracks billing accounts, node registry, dispatch mirrors, and account
locks so multiple workstations can share capacity without double-booking.

## When to use fleet mode

- You have more than one machine with coding agents installed (for example a
  MacBook orchestrator and a Mac Studio worker).
- You want the orchestrator to **preview** remote commands before `--exec`.
- You need **account locks** so two dispatches do not consume the same billing
  account concurrently.

Fleet mode is optional. Local-only dispatch (same machine as the orchestrator)
works without bootstrapping a fleet directory.

## Bootstrap

```bash
python3 scripts/goalflight_fleet.py bootstrap ~/.goal-flight/fleet
python3 scripts/goalflight_fleet.py validate --fleet-dir ~/.goal-flight/fleet
```

Set `GOALFLIGHT_FLEET_DIR` if you use a non-default path.

## Register a node

Add SSH-reachable workers with the node subcommand (see
`python3 scripts/goalflight_fleet.py node --help`). Each node record includes:

- SSH host alias (must match your `~/.ssh/config`)
- Repository checkout path on the remote machine
- Allowed agent transports (for example `codex-acp`, `cursor-agent`)

### Worker node install (Mac Studio / headless SSH)

Fleet SSH probes run under `BatchMode` with a thin default `PATH`. Install
agent CLIs once per worker, then run the PATH helper so doctor and billing
probes find them:

```bash
# On the worker (interactive SSH session)
curl -fsSL https://x.ai/cli/install.sh | bash          # Grok Build
./install.sh grok                                       # from ~/.goal-flight checkout
./install.sh claude-acp                                 # npm launcher + pinned fixed claude-acp build
bash scripts/hosts/fleet/setup_worker_path.sh         # ~/.local/bin symlinks + PATH
# Or: ./install.sh worker-path

# Codex / codex-acp / OpenCode via Homebrew are symlinked into ~/.local/bin by
# setup_worker_path.sh. Claude Code ships inside Claude.app (linked as `claude`);
# Claude **ACP** uses the separate npm shim `claude-code-cli-acp` (not `claude acp`).
# Until npm publishes claude-code-cli-acp >0.1.1, install builds upstream fix
# commit 14a5b0c from source by default, so workers need git, node/npm, and
# Rust cargo. Missing cargo fails loudly instead of leaving the broken 0.1.1
# binary in place. Temporary opt-out:
# GOALFLIGHT_SKIP_CLAUDE_ACP_PINNED_BUILD=1 ./install.sh claude-acp
# cursor-agent and grok are linked from their install trees into ~/.local/bin too.
```

Sign in on the worker (each person uses their own accounts; shared Grok/Cursor
is fine when you use separate worktrees/repos):

```bash
codex login                    # OpenAI
grok login --device-auth       # headless: URL + code in terminal
grok login --oauth             # local browser (auth.x.ai)
# Claude (subscription seat, headless): the browser first-run does NOT work over
# non-interactive ssh (no display; Keychain unreachable). Mint a long-lived token
# and persist it in THIS node's env instead:
claude setup-token             # interactive once; prints a long-lived token
#   add to the node's ~/.zshenv (auto-sourced by `ssh host cmd`):
#     export CLAUDE_CODE_OAUTH_TOKEN=<token>
#   verify: claude auth status --json  ->  authMethod=oauth_token / apiProvider=firstParty
#   do NOT validate with `claude -p` (always API-billed). On an interactive GUI
#   machine, plain `claude` / Claude.app first-run also works.
```

**Cursor over SSH:** `cursor-agent --version` may report “login keychain is
locked” in non-interactive SSH even when the GUI session is fine. Doctor treats
that as present; unlock once after reboot if needed:

```bash
security unlock-keychain ~/Library/Keychains/login.keychain-db
```

Validate SSH allowlisting before live dispatch:

```bash
python3 scripts/goalflight_fleet.py validate --fleet-dir ~/.goal-flight/fleet
python3 scripts/goalflight_doctor.py --project-root . --fleet
```

### Remote Claude worker (claude-acp) end-to-end

Claude's only supported remote surface is `claude-acp` on a **non-sandboxed** node
(the local / sandboxed shim is intentionally unsupported — no pty under the host
sandbox, Keychain unreachable over non-interactive ssh). One-time per worker:

1. `./install.sh claude-acp` — npm `claude-code-cli-acp` + the pinned `14a5b0c` build.
2. `claude setup-token` — interactive once; prints a long-lived subscription token.
3. Persist it in the node's `~/.zshenv` (auto-sourced by `ssh host cmd`):
   `export CLAUDE_CODE_OAUTH_TOKEN=<token>`. The detached fleet worker inherits it
   through the launch env allow-list — **the controller never injects it, so the
   node MUST hold it.**
4. `claude auth status --json` → expect `authMethod=oauth_token` /
   `apiProvider=firstParty` (subscription, not API). Do **not** use `claude -p`.
5. Register the node with `claude-acp` in its allowed transports, then dispatch:
   ```bash
   BASE_SHA="$(git rev-parse HEAD)"   # MUST be pushed: the node pins to it via fetch
   GOALFLIGHT_LIVE_SSH=1 python3 scripts/goalflight_fleet.py dispatch --node <node> \
     --agent claude-acp --billing-account anthropic/<acct> --base-sha "$BASE_SHA" \
     --dispatch-mode one-shot --prompt <p.md> --exec --json
   ```

Pair `--agent claude-acp` with an `anthropic/*` billing account, not an `openai/*`
one. `--dispatch-mode one-shot` avoids the goal-mode tool-smoke canary gate. The
fleet auth probe (`claude auth status --json`) gates dispatch and reds on a
revoked/expired token — re-run `claude setup-token` and refresh the env var.

## Operator flow

### 1. Preview dispatch (no SSH side effects)

```bash
BASE_SHA="$(git rev-parse HEAD)"
python3 scripts/goalflight_fleet.py dispatch \
  --node mac-studio \
  --agent codex-acp \
  --billing-account openai/default \
  --base-sha "$BASE_SHA" \
  --prompt README.md \
  --thin-defaults \
  --json
```

Inspect the planned remote command, worktree path, and `acp_run` invocation.
`--base-sha` is required for stale-clone protection: the node fetches and pins
the remote worktree to the controller-chosen commit before launch.

### 2. Execute live dispatch

```bash
export GOALFLIGHT_LIVE_SSH=1
BASE_SHA="${GOALFLIGHT_FLEET_BASE_SHA:-$(git rev-parse HEAD)}"
python3 scripts/goalflight_fleet.py dispatch \
  --node mac-studio \
  --agent codex-acp \
  --billing-account openai/default \
  --base-sha "$BASE_SHA" \
  --prompt README.md \
  --exec \
  --json
```

Live SSH is **opt-in**. Without `GOALFLIGHT_LIVE_SSH=1`, `--exec` refuses to run
so CI stays hermetic.

### 3. Watch and reconcile

```bash
python3 scripts/goalflight_fleet.py watch --fleet --once --json
# Block on one dispatch until its mirrored state is terminal (--until-terminal
# also requires --fleet):
python3 scripts/goalflight_fleet.py watch --fleet --until-terminal <dispatch-id> --timeout-s 600 --json
python3 scripts/goalflight_fleet.py reconcile --all-in-flight
```

`watch` mirrors remote `status.json` into the orchestrator register. `reconcile`
releases billing locks when dispatches reach terminal states (it prints a text
summary and does not take `--json`).

## Router entrypoint

The unified CLI surface is `bin/goalflight`:

```bash
bin/goalflight fleet dispatch read --help
bin/goalflight core doctor read
```

Action definitions live under `config/actions/`. Doctor reports router readiness
via `check_router` in `goalflight_doctor.py`.

## File transfer: ferry and salvage

`goalflight_fleet.py ferry` moves files between the controller and a node through
one envelope (explicit src/dst node+path, direction, purpose label recorded to the
receipt); `goalflight_fleet.py salvage` recovers a crashed worker's dirty worktree
by listing `git status --porcelain` over SSH, rsyncing only the dirty files into a
local quarantine, and re-listing before each pass until the file set converges
(divergence is reported as a liveness signal, not an error).

Pass `--dispatch-id` when salvaging a `salvage_needed` dispatch so the written
`salvage-manifest.json` records `dispatch_id`, `account_key`, and `fencing_token`
for the held account lock. After you have quarantined the dirty worktree, release
that lock explicitly — either run the `lock_release_command` field from the
manifest, or:

```bash
python3 scripts/goalflight_fleet.py salvage-complete --manifest <salvage-dir>/salvage-manifest.json
```

Manual release is also available when you have the exact lock identity:

```bash
python3 scripts/goalflight_fleet.py lock-release \
  --account-key <account_key> --fencing-token <fencing_token> --reason salvage_complete
```

Salvage-held locks are not TTL-reaped; they stay active until this post-salvage
release (or an operator `lock-release` with the matching fencing token).

**Credential safety.** Ferry refuses to transfer credential-shaped paths in either
direction — auth state, private keys, token/secret files — matched on the path, its
resolved target, and same-inode aliases, so a renamed, symlinked, or hardlinked
credential is denied. Push transfers rsync from a controller-owned staged copy taken
after the scan (so a post-scan swap on the live source cannot reach the wire); pull
transfers quarantine received bytes and scan them — including bounded content
signatures for private-key headers and provider auth-token JSON — before promoting.

**Documented residual.** The transfer layer does not defend against a *compromised
node*: a malicious process able to write unstructured credential bytes under an
innocent name while racing a single transfer can still move them — but such a process
can already read the credential directly, so the ferry is not the boundary in that
case. Ferry defends against accidental at-rest credential transit on a trusted node,
which is the fleet's threat model (each node runs its own operator's account).

## Live smoke test

Hermetic CI skips live SSH. For operator verification:

```bash
export GOALFLIGHT_LIVE_SSH=1
export GOALFLIGHT_FLEET_NODE=localhost   # or your SSH alias
./tests/manual/test_fleet_live_smoke.sh
```

## Failure triage

| Symptom | Check |
|---------|--------|
| SSH allowlist rejection | Dispatch plan command class; `goalflight_fleet_ssh.py` |
| Auth blocks `--exec` | `python3 scripts/goalflight_doctor.py --fleet --json` |
| Stuck billing lock | `reconcile --all-in-flight` |
| Remote status stale | `watch --once`; verify remote `.goal-flight/status/` |
| claude-acp auth red / `-32603` | `claude auth status --json` on the node; re-run `claude setup-token` and refresh `CLAUDE_CODE_OAUTH_TOKEN` in the node env |
| claude-acp pty / orphan shims | `bin/gf-reap-shims` (dry-run) then `--exec`; doctor `pty_shim_health` |

## Related docs

- Architecture overview: [architecture.md](architecture.md)
- Dispatch routing: `protocols/dispatch-routing.md` in the repository root
- Private runbooks (maintainer): `docs-private/runbooks/` (gitignored in skill repo)
