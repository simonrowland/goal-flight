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
execution, receipt creation, and transport. It receives the arm, SHA values,
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

`reattach` verifies the recorded remote `pid`, `start_token`, and `run_dir`,
then runs only the configured watch and collect commands. It never invokes the
run command. A local reattach timeout cancels by that exact identity before
returning a timeout result.

## Managed run leases

Each box names one durable `managed_run_directory`. The runner mints a unique
child for every run or export and passes that child as `{run_dir}`. `/tmp` and
other per-invocation scratch roots are invalid managed directories. The lease
record contains:

- `owner_identity` and `owner_pid`, identifying the local controller that owns
  the run;
- `lease_token`, an opaque fencing value returned in the arm result and passed
  to the remote kit; and
- `managed_run_directory` and `run_directory`, proving the path namespace.

The lease lock is a flock. Normal completion, timeout, cancellation, and
successful re-attach mark the record released; a killed owner drops the kernel
lock automatically. The reaper may mark an active record released only after a
process-identity check proves its recorded owner is dead. An unknown liveness
result is retained for manual review. `list` shows active and released
records, so a controller can inspect lease state without probing a live remote
host.

The configured transport is responsible for preserving the supplied run
directory and lease record in its remote kit. A launch log should include the
remote PID, start token, run directory, and, when available, the lease id,
lease token, and owner identity. Re-attach validates that the recorded run
directory remains below the configured managed root and uses the token only for
that exact run.

## Shared admission contract

Every project configuration for the same physical box must use the same
`admission.token_directory` and `boxes.<box>.token_key`. Each token is one
worker's P-core budget. Token files are persistent sentinel files whose locks,
not their contents or deletion, represent ownership. `flock` releases a token
when the holder exits, including SIGKILL.

Admission is granted only after both checks succeed:

- a token is held; and
- the measured remote `load1` is no greater than the configured P-core cap.

When either check fails, the runner sleeps for `queue_wait_seconds` and tries
again. It does not re-probe every token in a tight loop. The token is acquired
before command expansion, rendering, object pushing, or remote run creation.

An optional live cap file is re-read on every admission/census cycle:

```json
{
  "boxes": {
    "<box-id>": {"p_cores": 20, "token_pool_size": 4, "self_cap": 4}
  }
}
```

The optional `self_cap` at the top level or under a box is also re-read before
each arm. The remote load probe must return the answering hostname, not merely the
configured alias:

```json
{"hostname": "<answering-hostname>", "load1": 3.2, "p_cores": 20}
```

## Timeout, reaping, and health

The local timeout is a containment event. If the remote command emitted
`REMOTE_RUN_LAUNCHED pid=<pid> start_token=<token> run_dir=<dir>`, the runner
executes the configured cancel command before releasing its token. Cancellation
uses all three identity fields so a reused remote PID cannot be mistaken for
the original run.

The `reap` command scans durable running records. It cancels only records whose
local owner is gone and whose exact remote identity is available; incomplete
identity is reported for manual inspection. The `health` command records one
measured hostname/load sample and token census per configured box. These facts
make overloaded or orphaned boxes visible without treating a configured alias
as evidence.
