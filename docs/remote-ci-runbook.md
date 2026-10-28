# Remote CI runbook

The controller runs `scripts/goalflight_remote_ci.py`. Admission and run state
lives on the configured node; no shared filesystem mount is required. The node
needs the configured Python interpreter, Node, Git, flock, and `launchctl submit`
for a per-run coalition. Missing Python, Node, or Git makes the box unhealthy.

## Configuration (v2)

Commands are argv arrays. `remote_exec` is the sole transport primitive: execute
its `{script}` argument on the named box and return stdout, stderr, and exit
code. For SSH, use `["ssh", "{host}", "{script}"]`; SSH passes the snippet to the
remote shell. The controller installs the node helper under
`<managed-root>/helpers/remote-ci/` at a content-addressed path, verifies its
SHA-256 before every operation, and atomically reinstalls missing, mismatched,
or unreadable copies. Direct SSH operations reuse established connections where
available; otherwise they open a fresh connection.

`runner.command` is a **node-local foreground argv**, not an SSH command. The
wrapper submits the repository root, relative cwd, command, and allowlisted
environment through the laptop spool. Matched-pair gates use the existing
`goalflight_remote_ci` gate CLI. The laptop daemon
snapshots modified and untracked non-ignored files, deletions, renames, execute
bits, and symlinks into a commit. Once a box is selected and the run has a
lease, it asks that box for a lease-pinned confirmed bundle basis and creates
an incremental bundle when the basis is an ancestor of the snapshot. First
contact, unknown bases, and divergent history use a self-contained bundle. The
node verifies and fetches the bundle before confirming the new basis. The
bundle is shipped over the configured SSH transport. The Studio needs no
access to a remote Git repository. Ignored files are not shipped, so suites that depend on ignored
build output must build it. Tracked submodule gitlinks, Git LFS pointers, or
symlinks that target outside the snapshot (absolute or relative) run locally
with a fallback reason. Untracked nested Git checkouts are omitted from the
bundle. Goal Flight excludes its managed `worktrees/` directory in the
repository-local `.git/info/exclude`. `goalflight-remote-run` accepts one command and rejects
`--base` with a pointer to the matched-pair gate CLI. The node runs inside the
configured test requirements venv. Keep credentials out of configuration and
logs.

Workers invoke the installed wrapper with:

```shell
"$GOALFLIGHT_REMOTE_RUN" --kind gate --cmd-id <id> -- <command>
```

If `GOALFLIGHT_REMOTE_RUN` is unset, run the command locally.

For a command that must not inherit selected box variables, configure a
per-command environment override. Overrides apply on the node after the box
environment is assembled; the configured test requirements environment stays
enabled. Only `env_set` and `env_unset` are accepted:

```json
{
  "runner": {
    "heavy_command_ids": ["example-command-isolated"],
    "command_overrides": {
      "example-command-isolated": {
        "env_set": {"PYTHONPATH": "src"},
        "env_unset": ["EXAMPLE_DEP_ROOT", "EXAMPLE_DEP_EXTRACT_ROOT"]
      }
    }
  }
}
```

The command ID still selects the normal command grammar and can be marked
heavy for remote routing. Override values are not included in receipts.

```json
{
  "schema": "goalflight.remote-ci.config.v2",
  "paths": {
    "queue_dir": "<controller-state>/remote-ci/projects/<repo>/spool/requests",
    "state_dir": "<controller-state>/remote-ci/projects/<repo>/state",
    "result_dir": "<controller-state>/remote-ci/projects/<repo>/spool/results"
  },
  "daemon": {
    "lock_file": "<controller-state>/remote-ci/projects/<repo>/state/daemon.lock",
    "pid_file": "<controller-state>/remote-ci/projects/<repo>/state/daemon.pid",
    "poll_seconds": 5,
    "result_tail_kib": 16
  },
  "boxes": {
    "<box-id>": {
      "host": "<ssh-host-or-alias>",
      "expected_hostname": "<measured-hostname>",
      "python_interpreter": "/opt/homebrew/bin/python3",
      "remote_exec": ["ssh", "{host}", "{script}"],
      "p_cores": 28,
      "token_pool_size": 28,
      "managed_run_directory": "<absolute-node-managed-root>",
      "env": {"PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"}
    }
  },
  "repo": "<stable-repository-id>",
  "repo_url": "<private-git-url>",
  "test_requirements": "requirements-test.txt",
  "admission": {"queue_wait_seconds": 1, "max_wait_seconds": 30},
  "local_env": {"PATH": null},
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

Run one daemon and spool per project. Use the existing `repo` id as the
project id and path segment, and give each project's queue, state, results,
lock, and PID paths distinct locations. For example, a `pm2` project uses
`<controller-state>/remote-ci/projects/pm2/spool/{requests,results}` and
`<controller-state>/remote-ci/projects/pm2/state/{daemon.lock,daemon.pid}`.
The worker accepts only `queue_dir=<spool>/requests` and
`result_dir=<spool>/results`; a mismatch records
`fallback_reason=invalid-spool-layout` and follows the configured fallback
policy.
Existing goal-flight configs may keep their current shared-default spool; new
project configs use their own `projects/<repo>` paths. Keep the box
`managed_run_directory`, `p_cores`, and
`token_pool_size` identical across projects on the same Studio. `fallback`
defaults to `local`; `queue` waits up to `admission.max_wait_seconds` before a
typed remote-unavailable exit, while `fail` attempts immediate admission and
returns that exit without a local run. Set each box's `max_concurrent_seats`
between zero and `token_pool_size`; zero makes that project ineligible on the
box. Profile configs may set `runner.pytest_workers_cap`; larger `-n` values
and `-n auto` are rewritten to the cap, and a missing worker argument is set to
the cap so repository addopts cannot raise it.

The existing object form of `admission` keeps blocking enqueue behavior. To
enable resident node tickets for a project, set the field to the literal
`"box"`. The laptop then submits the snapshot bundle and returns to its request
queue while the Studio admits and runs the ticket. The daemon retains the
request checkpoint across restarts, collects the node receipt and output later,
and cancels the ticket if the wrapper is withdrawn or gives up. Only a
definitive submit rejection can use local fallback; accepted or ambiguous
tickets stay remote-owned. Deadlines count awake time, so laptop sleep does not
consume the ticket wait budget.

Local fallbacks inherit the caller environment and apply `runner.env` plus the
optional project-level `local_env` mapping. For every key declared in any box's
`env`, `local_env` must provide a local value or `null` to inherit the caller's
value. The example's box `PATH` therefore uses `{"PATH": null}` locally. Missing
keys fail config validation and refuse fallback with
`fallback_reason=profile-env-unavailable-locally`, naming the keys. This prevents
environment-gated tests from silently passing after their box environment is
lost.

`daemon.pid_file` may be null. Every project targeting one physical box must
use the **same node managed root and P-core/token defaults**, including through
different SSH aliases. The node pins that root in
`~/.goal-flight/remote-ci/authority.json`, creating the directory as the SSH
user. Authority and token-pool exclusivity are per SSH user. Use one SSH
account per Studio for all remote CI projects so they share the same pool.
When omitted, `managed_run_directory` defaults on the node to
`~/.goal-flight/remote-ci-managed`; an explicit config value takes precedence.
A request that names a different root is refused, so
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

Admission requires a free token, measured load/CPU headroom, and no refusal
from the host memory/CPU floor. A box is full only when CPU use is at least
95% **and** load1 is at least twice the physical core count. If either measure
still has headroom, admit work subject to the token cap and memory floor. This
is the owner rule (2026-10-19).
The 28-core Studio config uses a 28-token cap per box. Healthy boxes are ranked
by measured headroom, with ties spread across the fleet. Unknown samples do not
admit. The node retries its local token check at
`min(queue_wait_seconds, daemon.poll_seconds, 1)`. Admission wait is bounded by
`max_wait_seconds` (30 seconds by default). The wrapper runs the local command
and records the fallback reason when `fallback=local`. With
`fallback=queue`, the wrapper returns typed `RemoteUnavailable` after the
bounded wait and never runs locally; `fail` returns that typed result without
waiting for a seat. Other remote failures follow the same configured policy.
The matched-pair gate path keeps both arms together on the same verified box
or follows the configured fallback policy for both. The controller checks
queued-run status at the configured `daemon.poll_seconds`. After admission, the holder waits up to
`max(30, 2 × daemon.poll_seconds + 10)` seconds for `command.json` or
`release.json`, then releases the token itself, so the wait always outlasts
the controller's next poll.
That bounds a dropped enqueue or release reply. Tokens precede command
expansion, checkout, object movement, rendering, and test execution.

The controller's queued-run checks and active status watch sleep for the
configured `daemon.poll_seconds`. `result_tail_kib` controls the per-stream
tail in a completed status receipt; it defaults to 16 KiB and accepts 1–64
KiB. A receipt marks legacy retained runs with missing captures as unavailable.

### Project profiles

`boxes` is also the per-project host allowlist: a project can name only the
boxes it may use. Keep every project that targets one physical Studio on the
same managed root and P-core/token defaults.

Choose one `environment` recipe per project:

- `{"requirements_file": "requirements-test.txt"}` installs a repository
  relative file (including nested `-r`/`-c` inputs) into a cached venv.
- `{"requirements": ["numpy==2.4.6", "pytest==9.1.1"]}` installs inline
  package requirements into a cached venv.
- `{"interpreter": "${PROJECT_PYTHON}", "version_probe": [
  "${PROJECT_PYTHON}", "--version"]}` uses an already provisioned
  interpreter or venv. The probe's stdout is recorded in the receipt.

An opted-in profile is used by wrapper commands, targeted runs, and both arms
of a matched run. Profile setup and expected-output checks finish before pytest
starts. Inline requirements and every included line in a profile requirements
file must use exact `==` pins. A profiled command must match exactly
`<python> -m pytest [FLAGS] FILE [FILE ...]`. The only accepted flags, before
the files, are `-q`, `-v`, `-x`, `-rs`, `-rfE`, `--continue-on-collection-errors`,
`-p no:cacheprovider`, and `-n <integer>` (separate tokens). Each file must be a
relative path to an existing regular file in the repository. Absolute paths,
`..`, directories, `.`, globs, node ids, attached option values, options after a
file, missing files, and every other command form follow the configured fallback
policy with reason `command-outside-profile-grammar`. The node replaces the leading `python`,
`python3`, versioned Python, or `.venv/bin/python` executable with the profile
venv/interpreter. Profile `runner.command` and `runner.test_command` templates
may contain only that interpreter, `-m pytest`, and allowlisted flags; they
cannot contain test paths or selection placeholders.

`${NAME}` values in a recipe or probe resolve from that box's `env`. For the
file and inline recipes, the node records the base interpreter version and the
requirements hash and venv path. The old top-level `test_requirements` file
field is still read as a compatibility fallback; new project configs should
use `environment`.

`checkout_basename` optionally nests the repository checkout under that name
inside the assigned slot. The forwarded cwd follows it. Profile command env
uses this precedence: caller allowlisted variables, `runner.env`, box `env`,
then profile-derived values such as the checkout parent in `PYTHONPATH`. Leave
it empty when the project has no basename-dependent imports.

`base_overlays` declares files or directories installed into the base
generation before seats are cloned. Each entry has `kind` (`copy` or
`symlink`), an absolute `source` or `${NAME}` from the selected box's `env`,
and a safe checkout-relative `destination`. Overlays are re-applied when the
generation is prepared, so ignored machine-local files are present in every
fresh seat. A symlink keeps its configured absolute target. `receipt_probes`
run against the prepared checkout before the workload; nonzero exits,
launch errors, and expected-output mismatches fail setup with a typed
`profile-probe-failed` result.

For example, a project can declare:

```json
{
  "base_overlays": [
    {"kind": "copy", "source": "${PROJECT_DOCS_PRIVATE}", "destination": "docs-private"},
    {"kind": "copy", "source": "${EXAMPLE_DEP_CONFIG}", "destination": "example-data/config.toml"},
    {"kind": "symlink", "source": "${PROJECT_VENV}", "destination": ".venv"}
  ]
}
```

Configured output retention is unsupported and rejected with a typed config error.

Each box may declare `required_paths`, using absolute paths or `${NAME}` from
that box's `env`. The health preflight skips a box with a missing path and
includes the missing paths in the fallback reason. Use this for inputs
the project controller has already provisioned on that box. Pin identity is
checked with an expected-output `receipt_probe` before the workload starts;
missing or mismatched pins fail setup and follow the configured fallback
policy. Successful probe output and the selected environment version are
recorded under receipt metadata `profile` for every route.

`never_route` is a list of explicit, existing repository-relative files or
directories; glob characters make the profile invalid. Each selected file is
compared by filesystem identity with listed files and by parent containment
with listed directories. Profile routing is default-deny: every run must name
one or more existing files inside the repository and follow the command grammar
above. Directories, `.`, empty selectors, and arguments outside that grammar
follow the configured fallback policy.

`never_route` checks explicit request selections at admission; selections added
by repository pytest configuration are not covered, so use a project marker or
configuration for those selections.

The node does not provision external project inputs. The project controller
must configure their required paths and receipt probes. The example project policy
in `configs/remote-ci-example-sim.example.json` declares copied `docs-private`
and `example-data/config.toml` overlays, a `.venv` symlink, and the external
example dependency `PYTHONPATH`. It pins dependency configuration and cache versions, the imported example
dependency path and tree commit, and NumPy before the workload starts.
An absent or mismatched pin is a typed setup failure. Its `fallback=queue`
policy returns `RemoteUnavailable` after the bounded admission wait instead
of starting the command on the controller.

### Host-only profile acceptance checks

These checks need an SSH session to each allowed Studio and cannot be proven by
the hermetic fake node:

- Confirm the configured interpreter/version and imported dependency versions.
- Check every required external path and exact receipt-probe pin, including Git
  HEADs, the imported example dependency tree, and the example dependency config hash.
- Confirm installed dependencies and their reported versions match the
  project controller's local environment.
- Run each opted-in focused test locally and remotely on the allowed Studio,
  compare verdicts and relevant imported module paths, and confirm missing or
  mismatched inputs follow the configured fallback policy.
- Verify the allowlist excludes studio-1 for project-a/pm2 and excludes studio-1
  and studio-3 for example-sim.

One managed root per box holds the whole lifecycle:

```text
<managed-root>/admission/    tokens, tickets, policy, queue
<managed-root>/repos/<repo>/mirror.git
<managed-root>/repos/<repo>/bases/<commit>/          immutable base tree
<managed-root>/repos/<repo>/bases/current           atomic current commit
<managed-root>/repos/<repo>/seats/s-01 … s-N        APFS-cloned seats
<managed-root>/repos/<repo>/slot-meta/s-01.json   not inside the checkout
<managed-root>/runs/<lease-id>/
<managed-root>/results/index.jsonl
<managed-root>/keep/
<managed-root>/quarantine/
```

`N` is `token_pool_size`. Seats are re-created from the current base for each
run. A full pool queues; it does not create another directory. `keep/` is
never deleted by GC.

The node command receives `GOALFLIGHT_REMOTE_CI_SLOT_DIR` for the leased seat
and `GOALFLIGHT_REMOTE_CI_CHECKOUT_DIR` for the repository checkout inside it
(equal to the seat unless `checkout_basename` is set). It
also receives `GOALFLIGHT_REMOTE_CI_RUN_DIR` and
`GOALFLIGHT_REMOTE_CI_RESULT_INDEX`. Adapters (battery, pm2, kiln) must use
`checkout_dir` for repository operations and keep the checkout inside the
assigned slot. They must not `git worktree add` a per-SHA path or write a
checkout under `$HOME`. Migrating those scripts is a later change; this runner
is the contract they call. Project commands may add artifacts inside the run
directory but must not overwrite the holder's records.

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

Completed `status` replies keep `returncode` and add `streams.stdout` and
`streams.stderr` receipts: byte size, SHA-256, and a bounded tail. The text
view uses UTF-8 replacement decoding; `bytes_base64` preserves the exact tail.
Each tail reports `truncated` and `omitted_bytes`. Use the node `fetch` operation
with `stream: "stdout" | "stderr"` and `last_kb` to retrieve a compressed
tail on demand. Requests above 256 KiB are refused with the stream size,
requested bytes, and cap in the response. Fetch reads the retained run body;
the normal count and age retention rules are unchanged.

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
expands to an argv list for non-profiled runners. Profile runner templates use
the strict grammar above; the controller appends the validated selection after
the template flags. Corresponding `GOALFLIGHT_REMOTE_CI_*` environment values,
the lease record, and JSON selection/test lists reach the node runner.
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
python3 scripts/goalflight_remote_ci.py --config .goal-flight/remote-ci.json \
  cleanup-legacy-slots --box <box-id>
python3 scripts/goalflight_remote_ci.py --config .goal-flight/remote-ci.json \
  cleanup-legacy-slots --box <box-id> --apply
```

`run-once` consumes one request. The daemon alone writes results at
`result_dir/<request-id>.json`; workers write request files only. The wrapper
prints the receipt and returns the verified command exit status, or runs the
local command with `runner.timeout_seconds` after any non-verdict remote error.
A targeted GREEN never replaces the
controller's matched BASE/CANDIDATE gate. See
[the controller protocol](../protocols/remote-ci.md) for requests and receipts.

### Copy-on-write repo pool

The example project policy enables two seats each on `example-box` and
`studio-3`; `studio-1` is disabled for this pilot.

Each configured box stores one bare mirror per repo at
`<managed-root>/repos/<repo>/mirror.git`. The per-repo `pool.lock` serializes
bundle imports, base refreshes, seat clones, and collection. Immutable base
trees live at `bases/<commit>`; `bases/current` is an atomically replaced text
file containing the current commit. Temporary base materialization uses
`bases/.<commit>.<uuid>.tmp` and `bases/.<commit>.<uuid>.index`; pointer updates
use `bases/.current-<uuid>.tmp`.

Seats live at `seats/s-01`, `seats/s-02`, and so on. Setup removes the prior
seat tree and requires a same-volume APFS `clonefile` from the selected base;
there is no full-copy fallback. Each checkout has its own `.git` directory and
index, with `objects/info/alternates` pointing to the shared mirror. A
`checkout_basename` places the checkout under that directory; otherwise the
seat root is the checkout. Clone staging uses
`seats/.s-<nn>.clone-<uuid>`. The existing `slot-meta/s-<nn>.lock` and
`slot-meta/s-<nn>.json` files continue to fence seat ownership.

The mirror's confirmed basis is
`refs/goalflight/remote-ci/basis`; generation refs use
`refs/goalflight/remote-ci/base-generations/<commit>`. A basis requested by an
active run is pinned at
`refs/goalflight/remote-ci/bundle-bases/<lease-id>` until that lease is
released. Collection keeps those refs and generations reachable while a lease
or clone operation can use them.

New runs do not use the old full-copy path `slots/`. After the Studio soak,
first run cleanup without `--apply` on each pilot box and confirm it reports
`eligible`; then repeat with `--apply` to remove that box's legacy
`<managed-root>/repos/<repo>/slots` directory. The command refuses to remove
an active or unreadable slot tree.

## Identity and recovery

Before accepting a command, the node holder writes its PID, a node-issued
incarnation nonce (`start_token`), and run directory to `lease.json`. The nonce
is fenced by a kernel-held `holder.lock`; it is
not a controller-supplied PID claim. The record survives loss of the controller
or launch response. The controller does not keep a second copy of the lease.

Cancellation checks all three fields against the node record and requires
`expected_owner`. A stale controller cannot cancel a lease owned by a different
controller. When the wrapper falls back, it leaves a cancellation marker in the
spool; the daemon checks it while waiting and best-effort cancels the remote run.
Daemon startup and polling reap runs owned by a dead daemon. The wrapper ignores
a late remote result. The holder reads the
cancellation request and kills the workload's launchd
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
read, the lease stays `draining`. `clear` is the same kill for a holder
that is already dead. It writes `<managed-root>/admission/audit.log`. A live
holder is not cleared. The controller does not keep a request-state mirror
beside `result_dir`.

The reaper reads node leases directly. It acts only when the owning controller
PID is proven absent on its recorded controller hostname. A different
controller host or an unavailable liveness probe is unknown. Run recovery
from the original controller host. Completed node runs already release their
tokens; an incomplete pre-launch run can also be released by the reaper.

Local transport commands start in their own session. The detached node holder
also starts its own session; controller loss does not release admission while
the remote run continues. Both node and controller timeouts request exact
cancellation, and the node deadline still contains a run when the controller
has disappeared.

## First live run

Before routing the full gate, verify remote health for each configured Studio.
Admitted runs keep using the server that was verified at admission. Confirm
`/opt/homebrew/bin/python3 --version`, `node --version`, and
`git --version` work in the non-login SSH environment, and that the node can
write under `~/.goal-flight/remote-ci-managed` (or the configured managed root).
Source arrives in a Git bundle from the laptop, so the Studio does not need
repository access. Confirm package-index
access or a complete cache for the configured requirements, including nested
`-r`/`--requirement` and `-c`/`--constraint` files.

The full gate requires Node on every box. Set a per-box `env.PATH` or provision
the same supported Node version in the non-login SSH `PATH` on all boxes. A
missing Node makes a Studio ineligible and the wrapper runs the command locally.

Start only the laptop daemon, then run the worker command from
`protocols/worker-contract.md` and inspect its atomic result receipt. For the
first live gate, require `route=studio`, successful remote health verification,
the worker `HEAD` and snapshot SHA, and a non-empty requirements hash. Admitted
runs keep using the server that was verified at admission. Completed receipts
retain the admitted `box_id` and `expected_hostname` alongside the actual
`measured_hostname`. Check the
expected command id, box id, CPU/wall time, and load at start/end. A local-fallback
receipt is useful evidence that fallback works, but it does not verify the
Studio.
Before expanding the heavy-command allowlist, drill an SSH drop, daemon death,
Studio reboot, full managed volume, and an uncached SHA; each must resolve to
one local verdict and ignore any late remote result.

## Hermetic verification

`tests/python/test_remote_ci.py` supplies a scripted executor that runs the
shipped node helper against isolated fake-node directories with deterministic
load samples. It exercises real flocks, SIGKILL release, shared FIFO across
project daemons, launch/crash recovery, identity refusal, caps, receipts,
snapshot fallback, and timeout cancellation. Tests make no live remote, SSH,
or `ps` calls.
