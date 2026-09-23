# Remote CI runbook

This runbook operates the neutral runner from a project checkout. Replace every
`<...>` value with that project's paths and policy. Do not put credentials in
the config, request, receipt, or logs.

## Configuration

Create a per-project file such as `.goal-flight/remote-ci.json`. Commands are
argv arrays, never shell strings. The following is the complete schema shape;
the values are placeholders and are intentionally not a working host setup.

```json
{
  "schema": "goalflight.remote-ci.config.v1",
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
      "token_key": "<shared-physical-box-key>",
      "p_cores": 20,
      "token_pool_size": 4,
      "managed_run_directory": "<node-canonical-managed-run-directory>",
      "load_command": ["<ssh>", "{host}", "<load-probe>"],
      "env": {"<remote-env>": "<value>"}
    }
  },
  "admission": {
    "token_directory": "<shared-token-directory>",
    "queue_wait_seconds": 300,
    "live_cap_file": "<optional-shared-cap-file>"
  },
  "runner": {
    "command": ["<ssh>", "{host}", "<remote-runner>", "{sha}", "{selection}"],
    "watch_command": ["<ssh>", "{host}", "<remote-watcher>", "{run_dir}", "{pid}", "{start_token}"],
    "collect_command": ["<ssh>", "{host}", "<remote-collector>", "{run_dir}", "{pid}", "{start_token}"],
    "cancel_command": ["<ssh>", "{host}", "<remote-canceller>", "{run_dir}", "{pid}", "{start_token}"],
    "test_command": ["<test-interpreter>", "<test-runner>"],
    "env": {"<runner-env>": "<value>"},
    "timeout_seconds": 7200,
    "self_cap": 4,
    "self_cap_env": "<worker-cap-environment-name>",
    "chunk_size": 20,
    "verbose_option": "-v",
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

Required fields are all fields shown above except `daemon.pid_file` and
`admission.live_cap_file`, which may be `null`. `boxes` is a map of logical
boxes. `managed_run_directory` is an absolute, durable canonical directory on
the node; it must not be under `/tmp`. Every generated run directory is a
unique child of that root. `token_key` must be identical in every project
configuration sharing a physical box; `token_directory` must be the same
shared directory. The daemon creates token sentinel files and never removes
them.

Supported command placeholders are `{box}`, `{host}`, `{arm}`, `{sha}`,
`{tip_sha}`, `{candidate_sha}`, `{request_id}`, `{run_id}`, `{token_index}`,
`{lease_id}`, `{lease_token}`, `{owner_identity}`, `{managed_run_directory}`,
`{pid}`, `{start_token}`, `{run_dir}`, `{lock_dir}`, and `{reason}`. A token
equal to `{selection}`, `{test_files}`, or `{test_command}` expands to that
argv list. The runner also exports the corresponding lease, owner,
run-directory, and selection values through `GOALFLIGHT_REMOTE_CI_*` variables
and sets `self_cap_env` to `self_cap`. The configured transport must
create/preserve the supplied `run_dir` below `managed_run_directory` and carry
the lease record when it creates the remote kit.
The watch and collect commands are used only by `reattach`; they must not start
a new test run.

## Validate and start

```shell
python3 scripts/goalflight_remote_ci.py \
  --config .goal-flight/remote-ci.json validate

python3 scripts/goalflight_remote_ci.py \
  --config .goal-flight/remote-ci.json daemon
```

For a bounded controller smoke, use `run-once`. The daemon lock is an advisory
flock and releases on process exit; do not delete the lock file to stop a live
daemon.

## File a request

Workers should run targeted selections locally or through `kind: targeted`.
After review, the train/controller files one `kind: gate` request for the
consumer-wide selection. Use the request contract in
[`protocols/remote-ci.md`](../protocols/remote-ci.md), then submit it:

```shell
python3 scripts/goalflight_remote_ci.py \
  --config .goal-flight/remote-ci.json submit <request-file>.json
```

The command atomically places the validated request in `queue_dir`. The worker
completion message should name the request id, not claim a gate verdict.

## Read a result

The daemon writes `result_dir/<request-id>.json`. A gate is landable only after
the review verdict and a GREEN matched-pair result. A targeted GREEN is useful
worker evidence but does not replace the consumer-wide pair.

Every arm receipt must include the hostname that answered the remote probe.
Keep that measured hostname in the result for audit; do not render or substitute
the configured alias as if it were a measurement.

## Health and recovery

Run a census when a queue is unexpectedly long or a box is suspected of load
pressure:

```shell
python3 scripts/goalflight_remote_ci.py \
  --config .goal-flight/remote-ci.json health
```

The census reports measured hostname, load versus P-cores, and free/in-use
tokens. The live cap file can be changed by the operator; P-core/token values
are read on the next admission/census cycle and `self_cap` is read before the
next arm.

List durable remote leases, including their owner identity, lease token,
managed root, run directory, and release state:

```shell
python3 scripts/goalflight_remote_ci.py \
  --config .goal-flight/remote-ci.json list
```

After a controller crash, run the orphan reaper. It cancels only durable runs
whose local owner is gone and whose remote PID, start token, and run directory
are all recorded:

```shell
python3 scripts/goalflight_remote_ci.py \
  --config .goal-flight/remote-ci.json reap
```

An incomplete identity is a manual-review result. A lease is released during
owner-death cleanup only when the owner is proven dead; an unavailable process
probe remains `unknown` and retains the lease. Do not guess a remote PID or
delete token or lease files. For live incident work, the controller may use
`ps` to confirm local process identity and SSH to inspect the remote run; those
checks are intentionally outside the hermetic test gate.

If a kit contains `launch.log` but the local watcher died before producing a
result, reattach it before considering a rerun:

```shell
python3 scripts/goalflight_remote_ci.py \
  --config .goal-flight/remote-ci.json reattach <kit>/launch.log \
  --box <box-id> --request-id <request-id>
```

## Hermetic verification

The repository gate runs `tests/python/test_remote_ci.py` with a fake load
probe and executor. It covers config validation, managed lease paths and
owner-death proof, lease listing, token contention, kernel release after
SIGKILL, queue ordering, timeout cancellation, matched-pair ImportError
verdicts, recorded receipt parsing, chunking, orphan identity, and atomic
submission, session isolation, and reattach recovery. No new remote-CI test
needs a live SSH connection or `ps`.
