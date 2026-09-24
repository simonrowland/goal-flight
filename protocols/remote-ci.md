# Remote CI controller protocol

Goal Flight has one project-neutral remote-CI runner and gate daemon:
`scripts/goalflight_remote_ci.py`. A project supplies a JSON configuration; the
tool owns admission, queue order, arm lifecycle, receipts, and result files.
The configuration is the only place for host names, remote paths, test
commands, selection rules, and environment.

## Responsibility boundary

Workers run targeted tests only. A targeted request contains the worker's new
or changed test files and any explicitly named RED nodes, and is limited by
`selection.max_targeted_files` (20 by default). A worker does not pass a test
directory or duplicate the consumer-wide gate.

The train or controller runs the consumer-wide matched pair once, after the
review verdict exists:

1. `BASE` is the current tip's code with the candidate's test files overlaid.
2. `CANDIDATE` is the candidate commit with the same selection.
3. BASE adds `base_collection_option`, normally
   `--continue-on-collection-errors`, so a collection `ImportError` is retained
   as a RED result instead of being hidden by an early collection stop.
4. The verdict is GREEN only when both arms are green. A BASE collection
   ImportError is RED even when the process exit code and ordinary test summary
   are zero.

The configured remote command performs checkout, overlay construction, test
execution, and receipt creation on the node. The single configured
`remote_exec` primitive supplies transport. It receives the arm, SHA values,
test files, selection, and configured test command through argv placeholders and
`GOALFLIGHT_REMOTE_CI_*` environment variables.

## Request and result flow

The worker writes a request JSON file, validates and submits it, then returns a
completion marker naming the request id. The controller daemon consumes requests
in filename order; it is intentionally one queue runner, so waiting arms do
not create independent retry loops.

```text
worker -> request.json -> submit -> queue/<request_id>.json
                                      |
                                      v
                              one daemon queue runner
                                      |
                         shared token + load admission
                                      |
                         BASE then CANDIDATE (gate)
                                      |
                         results/<request_id>.json
                                      |
                         controller/worker reads verdict
```

Minimal request shape:

```json
{
  "schema": "goalflight.remote-ci.request.v1",
  "request_id": "request-<unique-id>",
  "kind": "gate",
  "tip_sha": "<tip-commit-sha>",
  "candidate_sha": "<candidate-commit-sha>",
  "test_files": ["tests/<consumer-test>.py"],
  "selection": ["tests/<consumer-test>.py", "-q"],
  "box": "<optional-box-id>",
  "purpose": "<single-line reason>",
  "worker": {
    "dispatch_id": "<dispatch-id>",
    "worktree": "<absolute-worker-tree>"
  }
}
```

`kind: targeted` runs only the candidate arm. `kind: gate` runs the matched
pair. The result is a JSON file with schema
`goalflight.remote-ci.result.v1`, `status` (`GREEN`, `RED`, or `ERROR`), both
arm records, sorted new/fixed node ids, and measured hostnames. The controller
waits for the result file; it does not infer a verdict from a process exit alone.

Every local watcher/driver is started in its own session (`setsid` semantics),
so stopping the queue runner does not kill the child while leaving its remote
run alive. If a watcher dies, use `reattach` with the kit's `launch.log`:

```shell
python3 scripts/goalflight_remote_ci.py \
  --config .goal-flight/remote-ci.json reattach <kit>/launch.log \
  --box <box-id> --request-id <request-id>
```

The node's launch log contains its PID, node-issued start token, run directory,
and lease token. Active reattach must prove that exact identity and inherit
the original holder's live admission token. Queued or dead holders cannot
bypass admission. Reattach observes the original run without executing a
second test command.

## Node admission and recovery

Every project sharing a physical box uses the same node managed root and
P-core/token defaults. Under `<managed-root>/admission/`, one numbered ticket
queue orders all projects. One node flock token is held for each active run.
Admission requires a free token and measured `load1 <= p_cores`; it precedes
command expansion, checkout, object movement, and rendering.

The node writes durable PID + incarnation token + run directory before it
accepts the command. The workload inherits the token flock, and the node also
keeps an explicit lease state. `admitted`, `running`, and `draining` reserve
the token index and the slot whether or not the flock is still held. Closing
a file descriptor does not release capacity. Cleanup sets `draining` under
the admission lock, kills the tree, and writes `released` under that same
lock only after the tree is proved dead. `UNKNOWN` stays `draining`. One
host-level authority file pins the managed root; a second root on that box
is rejected. Reaping and lease listing read node state through the same
`remote_exec` primitive, including leases still stored under
`<managed-root>/admission/runs/` from the layout before runs moved to
`<managed-root>/runs/`. There is no controller-side copy of the lease and no
request-state mirror. A live owner that has dropped the lease releases it on
its next reap or admission wait. Another host still treats that live owner
as busy. An admitted holder that never receives a command or a release drops
the token after 30 seconds. A holder killed before the workload starts is
released on the next admission once the slot is proved empty. After the
command deadline, reap kills the workload's own session: SIGTERM, a short
grace, then SIGKILL. `clear` does the same before the deadline and appends
an audit line.

Cancellation compares all identity fields, requires `expected_owner`, and asks
the proven holder to kill its own workload tree. It never signals a guessed
PID. A stale owner loses to reattach. Reaping also requires proof that the
owning controller is dead before it cancels a live holder; liveness from
another controller host or an unavailable probe is unknown and cannot
authorize that cancellation. A dead holder is reaped immediately: its
launchd job is removed and its tree is killed. The token stays until that
tree is proved dead. A cwd snapshot that misses the occupant, or that cannot
prove a matching pid is gone, keeps capacity. A launch intent is durable
before submit. An intent whose job is not listed is released only when the
run directory and the slot cwd are empty; otherwise it stays unknown. A
launchctl or cwd command that times out is unknown and keeps capacity. Past
the deadline the same kill records the deadline outcome.
Cleanup signals a process in a slot only when that slot's
current lease is the run being cleaned, so a stale reap cannot kill the next
occupant. An unresolved slot stays reserved.

The workload is a launchd job (`launchctl submit`), not a child of the
holder. The job label is written to the lease before submit, and the
workload waits until that lease also records the coalition id. launchd gives
that job its own resource coalition. Descendants
inherit the coalition across `fork`, `setsid`, and a parent exiting.
`proc_listcoalitions` is not in libproc. Members are enumerated without root
by `proc_listallpids` plus `proc_pidinfo` flavor 20
(`PROC_PIDCOALITIONINFO`): two `uint64` ids and three reserved fields, 40
bytes. The first id is the resource coalition. On this Mac (uid 501, no
root) a submitted `/bin/sleep` landed in coalition `(35944, 35945)` while
the login session was `(5738, 5739)` (891 processes). After the job's
parent exited, the setsid child was reparented to pid 1 and was still the
only member of the job coalition. A grandchild spawned from a `SIGTERM`
handler was in the same coalition. Cleanup signals each member only when
its pid, start time, and coalition still match the incarnation recorded for
it, including the `SIGKILL` after the grace period. A failed, partial, or
inconsistent member read is unknown, not an empty tree. Exit status is taken
from `launchctl list` before the job is removed; a missing status is not
success. A failed `launchctl remove` stays on the draining lease and reap
retries it. The login-session coalition
is never a kill target. If submit does not yield a private coalition, or
the pid list cannot be read, the lease stays `draining`. That is the
residual: a workload that was not launched as its own job cannot be named
without sweeping the session, so it is left held. A slot still owned by a
parent-version `slot/SLOT.json` or `slot/slot.lock` is not granted again.

Checkouts are the fixed slots `repos/<repo>/slots/s-01`…`s-N`, not a
directory per SHA and not under `$HOME`. The controller passes `repo` from
its config (omitted means `default`). Slot bookkeeping lives in
`repos/<repo>/slot-meta/`, outside the checkout, so a clean git tree is not
quarantined. The result index and its directory are fsync'd on every
append, before a body can be deleted. Released run bodies are
capped by age and count; `results/index.jsonl` keeps one line each. Token
sentinel files are not deleted. A conflicting cap still refuses enqueue; list
and reap do not, so a corrected config can recover.

A finished run names one of four outcomes in `result.json` `status`. They are not interchangeable, and a capacity refusal is never a dead workload:

| Outcome | `status` | exit | Meaning |
| --- | --- | --- | --- |
| Refused to start | `capacity-refused` | 75 | Retryable. Memory or CPU floor, or the pool cannot start the job. Not death. Exit 77 is not used. |
| Workload died | `died` | the process exit, or 2 when the process never produced one | The workload started and then failed, or launch itself failed. |
| Cancelled | `cancelled` | 130 | An owner cancelled the run. |
| Deadline | `deadline` | 124 | The run's deadline passed. |

The host guard refuses a start by writing `<managed-root>/admission/resource-floor.json` as `{"refuse": "capacity"}`. The node does not exec, frees the token, and records `capacity-refused`. A normal completion is `status: completed` with the workload's exit code.

Receipts must contain the answering node's measured hostname. Health returns
node-measured load, hostname, and actual token locks. Shared live caps reside
at `<managed-root>/admission/caps.json`.

Config v2 replaces local token/lease registries and operation-specific
watch/collect/cancel commands with one transport primitive and node supervision.
The disconnected automatic chunking helper and its unused settings were
removed. Bounded selections and explicit `-v` remain project policy.
See [the runbook](../docs/remote-ci-runbook.md) for the complete schema,
migration, identity fencing, and recovery commands.
