# Remote CI runbook

The controller runs `scripts/goalflight_remote_ci.py`. All admission and run
state lives on the configured node; no shared filesystem mount is required.
The node needs POSIX process groups, Python 3, and flock support.

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
different SSH aliases. The first call pins those defaults in
`<managed-root>/admission/policy.json`; conflicting callers fail closed.
The root must be absolute and durable, never under `/tmp`.

The shared directory contains `tokens/`, `tickets/`, a monotonic
`sequence.json`, `queue.lock`, and `runs/<lease-id>/`. Persistent token
sentinels must never be unlinked. One detached node holder owns a flock token
and a separate incarnation lock for its entire workload. SIGKILL releases its
kernel locks. Numbered tickets are allocated under the shared queue lock;
project request names and controller clocks do not determine cross-project
order. Abandoned tickets are removed only when their holder lock is free.

Admission requires a free token **and** node-measured `load1 <= p_cores`.
Unknown load/caps do not admit. The node polls admission at
`queue_wait_seconds`; cancellation remains responsive during that wait.
Tokens precede command expansion, checkout, object movement, rendering, and
test execution.

The node holder owns the run directory's lease, identity, command, output, and
result files. Project commands may add their artifacts there but must not
overwrite the holder's records or detach their test workload into another
process group.

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
behind `runner.command` as node-local argv. Remove `token_key`,
`load_command`, `admission.token_directory`, and
`admission.live_cap_file`; the node root is the shared authority.
Move live caps to the node file above.

Remove `watch_command`, `collect_command`, and `cancel_command`: the node
holder records output, watches completion, and cancels its own process group.
Remove `chunk_size` and `verbose_option`. Their former helper was never
connected to execution. The canon no longer claims automatic chunking;
project test policy can explicitly select `-v` and bounded selections.

Runner placeholders are `{box}`, `{host}`, `{arm}`, `{sha}`,
`{tip_sha}`, `{candidate_sha}`, `{request_id}`, `{run_id}`,
`{token_index}`, `{lease_id}`, `{lease_token}`, `{owner_identity}`,
`{managed_run_directory}`, `{run_dir}`, and `{overlay}`.
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
or launch response. Local running records are convenience mirrors only.

Cancellation checks all three fields against the node record. The holder
reads the cancellation request and kills **its own** process group. No
controller process signals a guessed or reused node PID. Unknown identity or
a dead holder without a completion result remains unknown/manual; the system
does not kill surviving processes by inference.

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
