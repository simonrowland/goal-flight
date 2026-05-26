# Multi-node fleet operations

Goal Flight 1.0 adds a **fleet layer** for dispatching ACP workers on remote
machines over SSH while the controller stays on your laptop or CI runner. The
fleet store tracks billing accounts, node registry, dispatch mirrors, and account
locks so multiple workstations can share capacity without double-booking.

## When to use fleet mode

- You have more than one machine with coding agents installed (for example a
  MacBook controller and a Mac Studio worker).
- You want the controller to **preview** remote commands before `--exec`.
- You need **account locks** so two dispatches do not consume the same billing
  account concurrently.

Fleet mode is optional. Local-only dispatch (same machine as the controller)
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

Validate SSH allowlisting before live dispatch:

```bash
python3 scripts/goalflight_fleet.py validate --fleet-dir ~/.goal-flight/fleet
python3 scripts/goalflight_doctor.py --project-root . --fleet
```

## Operator flow

### 1. Preview dispatch (no SSH side effects)

```bash
python3 scripts/goalflight_fleet.py dispatch \
  --node mac-studio \
  --agent codex-acp \
  --billing-account openai/default \
  --prompt README.md \
  --thin-defaults \
  --json
```

Inspect the planned remote command, worktree path, and `acp_run` invocation.

### 2. Execute live dispatch

```bash
export GOALFLIGHT_LIVE_SSH=1
python3 scripts/goalflight_fleet.py dispatch \
  --node mac-studio \
  --agent codex-acp \
  --billing-account openai/default \
  --prompt README.md \
  --exec \
  --json
```

Live SSH is **opt-in**. Without `GOALFLIGHT_LIVE_SSH=1`, `--exec` refuses to run
so CI stays hermetic.

### 3. Watch and reconcile

```bash
python3 scripts/goalflight_fleet.py watch --fleet --once --json
python3 scripts/goalflight_fleet.py reconcile --all-in-flight --json
```

`watch` mirrors remote `status.json` into the controller register. `reconcile`
releases billing locks when dispatches reach terminal states.

## Router entrypoint

The unified CLI surface is `bin/goalflight`:

```bash
bin/goalflight fleet dispatch read --help
bin/goalflight core doctor read
```

Action definitions live under `config/actions/`. Doctor reports router readiness
via `check_router` in `goalflight_doctor.py`.

## Live smoke test

Hermetic CI skips live SSH. For operator verification:

```bash
export GOALFLIGHT_LIVE_SSH=1
export GOALFLIGHT_FLEET_NODE=localhost   # or your SSH alias
./test/manual/test_fleet_live_smoke.sh
```

## Failure triage

| Symptom | Check |
|---------|--------|
| SSH allowlist rejection | Dispatch plan command class; `goalflight_fleet_ssh.py` |
| Auth blocks `--exec` | `python3 scripts/goalflight_doctor.py --fleet --json` |
| Stuck billing lock | `reconcile --all-in-flight` |
| Remote status stale | `watch --once`; verify remote `.goal-flight/status/` |

## Related docs

- Architecture overview: [architecture.md](architecture.md)
- Dispatch routing: `protocols/dispatch-routing.md` in the repository root
- Private runbooks (maintainer): `docs-private/runbooks/` (gitignored in skill repo)
