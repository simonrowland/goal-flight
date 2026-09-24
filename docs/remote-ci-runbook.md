# Remote CI runbook

The controller runs `scripts/goalflight_remote_ci.py`. All admission and run
state lives on the configured node; no shared filesystem mount is required.
The node needs Python 3, flock, and `launchctl submit` for a per-run coalition.

## Configuration (v2)

Commands are argv arrays. `remote_exec` is the sole transport primitive: execute
its `{script}` argument on the named box and return stdout, stderr, and exit
code. For SSH, use `["ssh", "{host}", "{script}"]`; SSH passes the snippet to the
remote shell. The helper is shipped inside the quoted snippet, so no helper
installation on the node is needed.

`runner.command` is a **node-local foreground argv**, not an SSH command. It
performs checkout/object fetching, BASE overlay, tests, and receipt generation
inside the admission token. Keep credentials out of configuration and logs.

```json
{
  "schema": "goalflight.remote-ci.config.v2",
  "paths": {
    "queue_dir": "<controller-state>/remote-ci/queue",
    "state_dir": "<controller-state>/remote-ci/state",
    "result_dir": "<controller-state>/remote-ci/results"
  },
  "daemon": {
    "lock_file": "<controller-state>/remote-ci/daemon.lock",
    "pid_file": "<controller-state>/remote-ci/daemon.pid",
    "poll_seconds": 5
  },
  "boxes": {
    "<box-id>": {
      "host": "<ssh-host-or-alias>",
      "remote_exec": ["ssh", "{host}", "{script}"],
      "p_cores": 20,
      "token_pool_size": 4,
      "managed_run_directory": "<absolute-node-managed-root>",
      "env": {}
    }
  },
  "repo": "<stable-repository-id>",
  "admission": {"queue_wait_seconds": 300},
  "runner": {
    "command": ["<node-runner>", "{sha}", "{run_dir}", "{selection}"],
    "test_command": ["<test-interpreter>", "<test-runner>"],
    "env": {},
    "timeout_seconds": 7200,
    "self_cap": 4,
    "self_cap_env": "<worker-cap-environment-name>",
    "base_collection_option": "--continue-on-collection-errors"
  },
  "selection": {
    "path_prefixes": ["tests/"],
    "allowed_options": ["-q", "-v", "--continue-on-collection-errors"],
    "value_options": ["-k", "-m"],
    "allowed_option_prefixes": [],
    "require_selector": true,
    "max_targeted_files": 20
  }
}
```

`daemon.pid_file` may be null. Every project targeting one physical box must
use the **same node managed root and P-core/token defaults**, including through
different SSH aliases. The node pins that root in
`/var/lib/goalflight/remote-ci/authority.json` (create the directory once,
owned by the node user). A request that names a different root is refused, so
two projects cannot open two token pools on one machine. The first call also
pins P-core and token defaults in `<managed-root>/admission/policy.json`. A
conflicting enqueue fails closed.
list, reap, cancel, and the other recovery operations still run, so a corrected
config can clean the box without hand-editing `policy.json`.
The root must be absolute and durable, never under `/tmp`. Optional `repo`
is a stable repository id. The controller passes it on enqueue. Slots and
result lines use it; a config that omits it uses `default`.

The shared directory contains `tokens/`, `tickets/`, a monotonic
`sequence.json`, and `queue.lock`. Run bodies live in
`<managed-root>/runs/<lease-id>/`. Leases left under
`<managed-root>/admission/runs/` are still listed, statused, cancelled, and
cleared, and a non-released one still reserves its token index. Persistent
token sentinels must never be unlinked. The holder keeps a separate
incarnation lock. The holder keeps the token flock; launchd cannot inherit
it. SIGKILL of the holder drops that flock, and the lease stays `running`
or `draining` until cleanup proves the coalition empty and writes
`released` under `queue.lock`. A second admission cannot
take the token or the slot while that lease is unresolved. Numbered tickets
are allocated under the shared queue lock;
project request names and controller clocks do not determine cross-project
order. Abandoned tickets are removed only when their holder lock is free.

Admission requires a free token **and** node-measured `load1 <= p_cores`.
Unknown load/caps do not admit. The node and the controller retry a free token
at `min(queue_wait_seconds, daemon.poll_seconds, 1)`, not at the raw queue
wait, so a freed token is not parked for minutes. Cancellation stays
responsive during that retry. After admission, the holder waits up to 30
seconds for `command.json` or `release.json`, then releases the token itself.
That bounds a dropped enqueue or release reply. Tokens precede command
expansion, checkout, object movement, rendering, and test execution.

One managed root per box holds the whole lifecycle:

```text
<managed-root>/admission/    tokens, tickets, policy, queue
<managed-root>/repos/<repo>/slots/s-01 … s-N
<managed-root>/repos/<repo>/slot-meta/s-01.json   not inside the checkout
<managed-root>/runs/<lease-id>/
<managed-root>/results/index.jsonl
<managed-root>/keep/
<managed-root>/quarantine/
```

`N` is `token_pool_size`. Slots are created once and reused. A full pool queues;
it does not create another directory. A dirty git slot is moved into
`quarantine/` and the slot directory is reused empty. `keep/` is never
deleted by GC.

The node command receives `GOALFLIGHT_REMOTE_CI_SLOT_DIR`,
`GOALFLIGHT_REMOTE_CI_RUN_DIR`, and `GOALFLIGHT_REMOTE_CI_RESULT_INDEX`
(`{slot_dir}` / `{checkout_dir}` is the slot). Adapters (battery, pm2, kiln)
must checkout `--detach <sha>` inside that slot only. They must not
`git worktree add` a per-SHA path or write a checkout under `$HOME`. Migrating
those scripts is a later change; this runner is the contract they call.
Project commands may add artifacts inside the run directory but must not
overwrite the holder's records.

`result.json` `status` is one of `capacity-refused` (exit 75, retryable, not a dead workload), `died` (the workload's own exit, or 2 if it never produced one), `cancelled` (exit 130), or `deadline` (exit 124). Exit 77 is not a capacity refusal. A host memory or CPU guard refuses the start by writing `<managed-root>/admission/resource-floor.json`:

```json
{"refuse": "capacity"}
```

The node then does not exec, releases the token, and records `capacity-refused`. A normal exit is `status: completed`.

On release the node appends one JSON line to `results/index.jsonl`
(`run_id`, `repo`, `sha`, `slot`, `host`, `start`, `finish`, `status`,
`exit_code`, `body_path`, `bytes`), fsyncs that file, and fsyncs the results
directory on every append, before any body is removed. GC deletes only
a released body that already has that line, has no live `pid + start_token +
run_dir`, and has no process whose cwd is the run directory. Successful
bodies are kept for 7 days and the newest 20. Other terminal bodies are kept
for 14 days and the newest 50. Unknown, unreadable, or live bodies stay.
`gc` prints `class`, `bytes`, and `path`; `gc --apply` deletes eligible
bodies. `tokens/` is never deleted.

Operators may lower shared caps through
`<managed-root>/admission/caps.json`:

```json
{"p_cores": 16, "token_pool_size": 3, "self_cap": 2}
```

P-core/token caps cannot exceed the pinned defaults. All projects read this
same file on admission/census. `self_cap` limits the per-project setting; an
admitted arm uses the sample taken at admission. Existing workloads finish
under their original admission.

## Migration from v1

Set schema to v2 and add `boxes.<id>.remote_exec`. Move the old remote command
behind `runner.command` as node-local argv. v2 rejects `token_key` and
`load_command` (they used to gate admission and are not implied by
`getloadavg`), and rejects `admission.token_directory` and
`admission.live_cap_file`; the node root is the shared authority.
Move live caps to the node file above.

Remove `watch_command`, `collect_command`, and `cancel_command`: the node
holder records output, watches completion, and cancels that job's coalition.
Remove `chunk_size` and `verbose_option`. Their former helper was never
connected to execution. The canon no longer claims automatic chunking;
project test policy can explicitly select `-v` and bounded selections.

Runner placeholders are `{box}`, `{host}`, `{arm}`, `{sha}`,
`{tip_sha}`, `{candidate_sha}`, `{request_id}`, `{run_id}`,
`{token_index}`, `{lease_id}`, `{lease_token}`, `{owner_identity}`,
`{managed_run_directory}`, `{run_dir}`, `{checkout_dir}`, and `{overlay}`.
A whole argument `{selection}`, `{test_files}`, or `{test_command}`
expands to an argv list. Corresponding `GOALFLIGHT_REMOTE_CI_*` environment
values, the lease record, and JSON selection/test lists reach the node runner.
Only configured environment values are forwarded to the workload, not the
controller's entire environment.

## Commands

```shell
python3 scripts/goalflight_remote_ci.py --config .goal-flight/remote-ci.json validate
python3 scripts/goalflight_remote_ci.py --config .goal-flight/remote-ci.json daemon
python3 scripts/goalflight_remote_ci.py --config .goal-flight/remote-ci.json submit <request.json>
python3 scripts/goalflight_remote_ci.py --config .goal-flight/remote-ci.json health
python3 scripts/goalflight_remote_ci.py --config .goal-flight/remote-ci.json list
python3 scripts/goalflight_remote_ci.py --config .goal-flight/remote-ci.json reap
python3 scripts/goalflight_remote_ci.py --config .goal-flight/remote-ci.json clear \
  --box <box-id> --run-dir <run-dir> --lease-token <lease-token> \
  --pid <holder-pid> --start-token <start-token>
python3 scripts/goalflight_remote_ci.py --config .goal-flight/remote-ci.json gc
python3 scripts/goalflight_remote_ci.py --config .goal-flight/remote-ci.json gc --apply
```

`run-once` consumes one request. Results remain at
`result_dir/<request-id>.json`. A targeted GREEN never replaces the
controller's matched BASE/CANDIDATE gate. See
[the controller protocol](../protocols/remote-ci.md) for requests and receipts.

## Identity and recovery

Before accepting a command, the node holder writes its PID, a node-issued
incarnation nonce (`start_token`), and run directory to `lease.json` and
`launch.log`. The nonce is fenced by a kernel-held `holder.lock`; it is
not a controller-supplied PID claim. The record survives loss of the controller
or launch response. The controller does not keep a second copy of the lease.

Cancellation checks all three fields against the node record and requires
`expected_owner`. The owner is the one this controller admitted or attached
under, not a fresh read of the lease. A reattach changes the owner; a stale
controller's cancel then returns owned and does not write `cancel.json`. The
holder reads the cancellation request and kills the workload's launchd
coalition, one pid at a time. It re-reads coalition membership together with
that pid's start time before SIGTERM and again before SIGKILL, and does not
signal a pid whose incarnation changed. No controller process signals a
guessed or reused node PID. The job label is stored on the lease before
`launchctl submit`, and the workload does not exec until the coalition id is
stored too. Exit status is read from `launchctl list` before the job is
removed. A missing status is not success. A failed `launchctl remove` leaves
the label on the draining lease so reap can retry it.

Unknown identity stays unknown and is kept. A dead holder is kept until the
command deadline stored on the lease. After that deadline, reap sets the
lease to `draining` and signals every member of the coalition recorded when
the workload was submitted to launchd. Descendants stay in that coalition
across `setsid` and after the parent is reparented. The login session's
coalition is not a kill target. Each member's pid and start time is kept on
the lease and checked again, with its coalition, before SIGKILL. A failed or
partial member read is unknown, not an empty tree. The token is released only
after a complete enumeration shows nobody left and the launchd job is verified
gone. If the job never got a private coalition, or the pid list cannot be
read, the lease stays `draining`. A parent-version
slot with `slot/slot.lock` held or `slot/SLOT.json` for a lease that is not
released is not granted to a new run. `clear` is the same kill for a holder
that is already dead. It writes `<managed-root>/admission/audit.log`. A live
holder is not cleared. The controller does not keep a request-state mirror
beside `result_dir`.

The reaper reads node leases directly. It acts only when the owning controller
PID is proven absent on its recorded controller hostname. A different
controller host or an unavailable liveness probe is unknown. Run recovery
from the original controller host. Completed node runs already release their
tokens; an incomplete pre-launch run can also be released by the reaper.

Copy the node's `launch.log` to the controller and reattach:

```shell
python3 scripts/goalflight_remote_ci.py --config .goal-flight/remote-ci.json \
  reattach <kit>/launch.log --box <box-id> --request-id <request-id>
```

Active reattach inherits the original token only after proving the node
identity and live holder. A queued holder has no token to inherit. A dead
holder is rejected. Reattach never starts another workload. Completed runs
can be collected without admission because they have no remaining workload.

Local transport commands start in their own session. The detached node holder
also starts its own session; controller loss does not release admission while
the remote run continues. Both node and controller timeouts request exact
cancellation, and the node deadline still contains a run when the controller
has disappeared.

## Hermetic verification

`tests/python/test_remote_ci.py` supplies a scripted executor that runs the
shipped node helper against isolated fake-node directories with deterministic
load samples. It exercises real flocks, SIGKILL release, shared FIFO across
project daemons, launch/crash recovery, identity refusal, reattach, caps,
receipts, and timeout cancellation. Tests make no live remote, SSH, or
`ps` calls.
