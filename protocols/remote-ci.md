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
accepts the command. Its detached holder owns the token for the entire
workload, including controller crashes. SIGKILL releases the holder's kernel
locks. Local daemon records are convenience mirrors; reaping and lease listing
read authoritative node state through the same `remote_exec` primitive.

Cancellation compares all identity fields and asks the proven holder to kill
its own group. It never signals a guessed PID. Reaping also requires proof
that the owning controller is dead; liveness from another controller host or
an unavailable probe is unknown and cannot authorize cancellation. A dead node
holder without completion evidence remains unknown/manual.

Receipts must contain the answering node's measured hostname. Health returns
node-measured load, hostname, and actual token locks. Shared live caps reside
at `<managed-root>/admission/caps.json`.

Config v2 replaces local token/lease registries and operation-specific
watch/collect/cancel commands with one transport primitive and node supervision.
The disconnected automatic chunking helper and its unused settings were
removed. Bounded selections and explicit `-v` remain project policy.
See [the runbook](../docs/remote-ci-runbook.md) for the complete schema,
migration, identity fencing, and recovery commands.
