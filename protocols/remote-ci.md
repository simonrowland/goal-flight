# Remote CI controller protocol

Goal Flight has one project-neutral remote-CI runner and gate daemon:
`scripts/goalflight_remote_ci.py`. A project supplies a JSON configuration; the
tool owns admission, queue order, arm lifecycle, receipts, and result files.
The configuration is the only place for host names, remote paths, test
commands, selection rules, and environment.

## Responsibility boundary

Sandboxed workers submit through the state-dir spool; only the laptop daemon
uses `remote_exec` to contact the Studios. Requests and results are atomically
published under `remote-ci/spool/{requests,results}`. Workers submit requests;
the laptop daemon normally writes results. Peer-worker tampering with result
files is outside the routing threat model. Unique request IDs prevent accidental
cross-talk, and local fallback receipts use `*.local.json`, which the daemon
skips when scanning requests.
The worker-facing command is:

```shell
"$GOALFLIGHT_REMOTE_RUN" --kind gate --cmd-id goalflight-full-gate -- ./tests/run.sh
```

Invoke the runner directly. Do not wrap the command in `bash -lc` or another
login shell: it can replace the configured interpreter path with a Python
environment that lacks pytest, leaving the run without verifiable test nodes.

If `GOALFLIGHT_REMOTE_RUN` is unset, run the command locally.

The wrapper atomically submits the repository root, cwd relative to that root,
argv, and allowlisted environment. Matched-pair gates use the existing
`goalflight_remote_ci` gate CLI. The laptop daemon snapshots the current
worktree into a commit before it contacts a Studio. Once a box is selected and
the run has a lease, it asks that box for a lease-pinned confirmed bundle basis
and creates an incremental bundle if that basis is an ancestor of the
snapshot. First contact, unknown bases, and divergent histories use a
self-contained bundle. The node verifies and fetches the bundle before
confirming the new basis. The
private Git index includes modified files, non-ignored untracked files,
deletions, renames, executable-bit changes, and symlinks without changing the
worker's index, branch, or refs except for one temporary create-only keep ref.
Ignored build output is not shipped and suites that need it must build it. The
receipt records `HEAD`, snapshot SHA, and whether the worktree was dirty. The
arm result includes `bundle_bytes`, counting the transferred bundle (zero
when the mirror already has the requested commit). Its receipt reports parsed
`passed`, `failed`, and `skipped` test counts. The Studio receives the bundle
through the laptop daemon's SSH transport and does not need remote-repository
access. The wrapper forwards only these caller
environment variables: `CI`, `EXERCISE_FAIL`, `LANG`, `LC_ALL`, `NODE_OPTIONS`,
`PYTHONHASHSEED`, `PYTHONPATH`, and `TZ`. The node sets `TMPDIR` inside the
leased slot. Repositories containing submodules, Git LFS pointers, or symlinks
that target outside the snapshot (absolute or relative) run locally with a
fallback reason; v1 does not ship those paths. The ordinary
`goalflight-remote-run` form accepts one command. Matched pairs use:

```shell
"$GOALFLIGHT_REMOTE_RUN" --pair --base <base-sha> --cmd-id goalflight-full-gate -- <pytest command>
```

Use the same direct invocation for matched pairs; do not wrap it in a login
shell, which can replace the configured interpreter path.

The pair snapshots the caller's current `HEAD` and worktree as candidate,
submits both arms through the queue, and pins both arms to one selected Studio.
It prints per-arm box and pass/fail counts, sorted candidate-only and fixed
node IDs, and the result path. `--json` emits the same summary as JSON. The
command exits nonzero when either arm is RED, including a RED receipt with no
candidate-only failures; it exits zero only when both arms are GREEN.
`--recheck N` reruns every
candidate-only failing node `N` times per arm in base/candidate order on that
same box; results classify each node as `regression`, `flake`, or
`base-failing`. If either arm cannot complete remotely, a matched pair refuses
with the reason and never runs either arm locally.

In the default blocking admission mode, every failure before a verified command
exit falls back to the local command: daemon/config or spool failure, no
healthy box, expired token wait, source ship, provisioning, SSH, node, or setup
failure. The local run honors `runner.timeout_seconds`, and its receipt records
`route=local-fallback` and the reason. `GOALFLIGHT_REMOTE_CPU_ROUTE=local`
forces `route=local`.

Any missing, unverified, or ambiguous ordinary command outcome is resolved
locally. The daemon best-effort cancels the remote run, but a late result never
replaces the local verdict. Request IDs are unique to prevent an old result from
being mistaken for a newer request; duplicate execution after an ambiguous
transport failure is allowed.

The object form of `admission` retains the default blocking behavior. With
`"admission": "box"`, the daemon streams the snapshot to node `submit`, then
returns to the laptop request queue and collects status and output on later passes.
Node acceptance transfers execution ownership. Local fallback is allowed only
after a definitive submit rejection and when `fallback` is `local`; an
ambiguous submit remains unresolved and is retried with the same request and
arm identity. An accepted ticket is never re-run locally.

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
4. Both arms use the same verified box, or both run locally if either remote
   arm cannot produce a clean verdict. A completed remote BASE is never mixed
   with a local CANDIDATE.
5. The verdict is GREEN only when both arms are green. A BASE collection
   ImportError is RED even when the process exit code and ordinary test summary
   are zero. A RED matched-pair gate exits with the first nonzero candidate/base
   command status, or `1` when receipt validation makes the verdict RED while
   both command statuses are zero. A BASE-arm failure exits nonzero. GREEN
   exits with the candidate command status.

The worker-facing `--pair` command runs the exact base commit, including its own test files, and reports candidate-only node IDs missing at base as `absent-at-base`. The configured train pair keeps the consumer-wide behavior above: it runs the tip code with candidate test files overlaid.

The configured remote command performs checkout, overlay construction, test
execution, and receipt creation on the node. The single configured
`remote_exec` primitive supplies transport. It receives the arm, `HEAD` and
snapshot SHA values, dirty flag, relative cwd, allowlisted environment, test
files, selection, and configured test command through argv placeholders and
`GOALFLIGHT_REMOTE_CI_*` environment variables.

### Controller-to-node protocol

The controller uses node protocol version 5. When the daemon constructs its
`RemoteRunner`, it reads `goalflight_remote_ci_node.py` once and keeps those
bytes for the daemon's lifetime. Each operation hashes those cached bytes; if
the node needs an install, the controller ships that cached source. The node
launcher hashes the content-addressed copy under
`<managed-root>/helpers/remote-ci/` and executes only the bytes that match the
expected hash. A missing, mismatched, unreadable, or unverifiable copy is
unknown and triggers an atomic reinstall through a same-directory temporary
file, file `fsync`, and `os.replace`. Concurrent installers write the same
digest-specific target; only a complete, re-verified file is executed. This
prevents derived helper state from outliving a changed source file and
silently running stale code.

Roll out protocol v5 in this order:

1. Check the controller skill out at the release commit while the current
   daemon remains running. Updating files does not change the code already
   loaded by that process.
2. **BOOTSTRAP (first rollout):** daemons predating protocol v5 do not
   understand `drain.request` and cannot drain themselves. They do not
   write `daemon.status.json`; the old PID is in the configured `daemon.pid_file`
   (`daemon.pid`). Set `CFG` to the project's config and `PROJECT` to its
   LaunchAgent project name. This script polls every 15 seconds, requires an
   empty request spool (ignoring `.local.json` receipts), and checks the
   configured repo's leases. An unreadable row or a row without `repo` is busy.
   It repeats both checks immediately before kickstart and prints the old PID
   from the configured pid file:

   ```sh
   CFG=<project-config>
   PROJECT=<project>
   python3 - "$CFG" "$PROJECT" <<'PY'
   import json, os, subprocess, sys, time
   from pathlib import Path
   config_path = Path(sys.argv[1])
   project = sys.argv[2]
   config = json.loads(config_path.read_text())
   requests = Path(config["paths"]["queue_dir"])
   repo = config.get("repo") or "default"
   pid_file = Path(config["daemon"]["pid_file"])

   def quiescent():
       queued = [path for path in requests.glob("*.json")
                 if not path.name.endswith(".local.json")] if requests.exists() else []
       if queued:
           return False
       listed = subprocess.run(
           [sys.executable, "scripts/goalflight_remote_ci.py",
            "--config", str(config_path), "list"],
           check=True, capture_output=True, text=True,
       )
       leases = json.loads(listed.stdout)
       if not isinstance(leases, list):
           return False
       for row in leases:
           if not isinstance(row, dict):
               return False
           row_repo = row.get("repo")
           state = row.get("state")
           if (not isinstance(row_repo, str) or not row_repo
                   or not isinstance(state, str)
                   or state.casefold() == "unreadable"):
               return False
           if row_repo == repo and state.casefold() != "released":
               return False
       return True

   while True:
       if not quiescent():
           time.sleep(15)
           continue
       pid_record = json.loads(pid_file.read_text())
       old_pid = pid_record.get("pid")
       if not isinstance(old_pid, int) or isinstance(old_pid, bool) or old_pid <= 0:
           raise SystemExit(f"invalid daemon PID in {pid_file}")
       if not quiescent():
           time.sleep(15)
           continue
       print(f"old daemon PID: {old_pid}", flush=True)
       subprocess.run([
           "launchctl", "kickstart", "-k",
           f"gui/{os.getuid()}/com.goalflight.remote-ci.{project}",
       ], check=True)
       break
   PY
   ```

   Under continuous load, reaching both empty conditions may take a
   while. There is a small residual race: a request can be claimed between the
   final checks and the kickstart. The final checks are immediately followed
   by the kickstart, so that interval is short, but the check and kill are not
   atomic; detect this case by checking lease records for the same
   `request_id` having run twice.

   After bootstrap, verify `daemon.status.json` reports `running` with a PID
   different from the saved old PID, and verify
   `git -C <deployed-checkout> rev-parse HEAD` is the intended release commit.
3. On later deployments, request a graceful daemon drain by writing the
   current daemon PID to `<paths.state_dir>/drain.request`.
   The daemon stops claiming spool requests, finishes requests it already owns,
   publishes their result files, and exits with status 0. Requests still in the
   spool are left for the replacement daemon. Before requesting drain, record
   the PID from the current `running` status, then write that PID into the flag:

   ```sh
   STATE_DIR=<project-state-dir>
   STATUS="$STATE_DIR/daemon.status.json"
   OLD_PID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["pid"])' "$STATUS")
   printf '%s\n' "$OLD_PID" > "$STATE_DIR/drain.request"
   ```

   A flag naming this daemon makes it drain. After acquiring the project lock,
   startup removes an existing flag unless it names this daemon's PID, then
   updates the configured PID file and publishes `running`. Cleanup precedes
   both writes, so an operator cannot target the successor before cleanup.
   Empty or unparsable content written after `running` targets the current
   daemon. Both claim checks use the same rule: drain only when the flag names
   this PID or its content is malformed. A flag naming another PID never drains
   this daemon. It finishes requests it already owns, publishes their result
   files, and exits with status 0.
   Requests still in the spool are left for the replacement daemon. The
   `health` command also reports `daemon.status` and
   `daemon.drain_timeout_reported`. Wait for the
   old process to exit. The LaunchAgent
   `com.goalflight.remote-ci.<project>` then restarts the daemon on the deployed
   code. Verify `<state-dir>/daemon.status.json` reports `status: "running"`
   with a PID different from the recorded old PID, and verify
   `git -C <deployed-checkout> rev-parse HEAD` is the intended release commit.
   Per-lease node jobs share the label prefix; they are not the controller
   daemon and must not be restarted for this rollout.
4. The next SSH operation installs the v5 node helper by content hash.
5. Verify remote health on each Studio before resuming dispatch. No Studio-side
   service restart is needed because the node helper is shipped by content hash
   on the next SSH operation. At deploy time,
   check whether the deployed train line has a resident node service; if it does,
   identify and update or restart that service as part of the rollout.

If a queued request's 8,820-second client result budget expires during a long
drain, the client cancels it as `remote-unavailable`; it is not run locally.

If a drain is still active after 30 minutes, `daemon.status.json` and `health`
report `drain_timeout_reported: true`, and the daemon writes a timeout to
stderr. The LaunchAgent's `StandardErrorPath` is
`~/Library/Logs/goal-flight/remote-ci-<project>.log`. Leave the process alone
and continue waiting: it keeps its owned requests and their publication, then
exits 0 when they finish so the LaunchAgent can restart it.
A clean replacement startup removes a stale `drain.request` after taking the
project lock and before publishing `running`; a drain request created for that
replacement after `running` remains in place and drains that daemon.

Restarting a controller agent constructs a new runner and loads the updated
helper bytes; updating files under a running agent does not change the code it
ships.

Every operation response emitted by the installed node helper carries
`protocol_version: 5`. Version 5 retains the `bundle-basis`
operation from v4, records the measured hostname in health responses and
leases, and verifies the returned lease identity before work starts. The
controller rejects a version mismatch or an unversioned legacy response with a
protocol error. It never falls back to shipping the
helper inline or accepting full stdout/stderr bodies. The node rejects requests
that do not name version 5. Enqueue requests
carry the controller's configured `daemon.poll_seconds`. After admission, the
holder waits for `command.json` for the basis lookup (30 seconds), bundle
preparation (300 seconds), bundle upload (300 seconds), and a polling margin of
`max(30, 2 × poll_seconds + 10)` seconds. These bounds match the controller's
local bundle operations without extending the admission budget. Direct SSH operations reuse established connections where available;
otherwise they open a fresh connection.

Controller status checks for queued runs and active workloads honor
`daemon.poll_seconds`. The node's local token-file retry remains capped at one
second because it does not cross SSH and keeps a freed token responsive.

After an enqueue returns beyond the admission deadline, the controller reads
lease status before deciding whether to start or withdraw: admitted or later
leases continue through the normal start/watch path, and only queued leases are
withdrawn. Release and command start serialize on `command.lock`. If a holder
admits between the status read and release, release wins before command start
and the controller falls back for a run that never started; the race cannot
start a second workload. Each daemon's cached helper source matches the
controller code loaded by that daemon; use the stop-update-start deploy order
above so the controller and helper are upgraded together.

### Bounded result streams

For every terminal state, including `died`, `status` returns the normal
exit/result fields plus
`streams.stdout` and `streams.stderr`. Each stream receipt contains
`available`, `size_bytes`, `sha256`, and
`tail: {text, bytes_base64, truncated, omitted_bytes}`. The text view is
UTF-8-decoded with replacement; `bytes_base64` preserves the exact tail bytes.
Legacy retained runs without a capture report `available: false` and the
SHA-256 receipt for an empty stream.
The configurable `daemon.result_tail_kib` defaults to 16 KiB and is capped at 64
KiB. The node hashes each file in bounded memory and retains only the requested
tail in the status reply.

The explicit node operation `fetch` accepts `stream: "stdout" | "stderr"`
and `last_kb`. Successful replies contain a gzip-compressed, base64-encoded
tail plus `truncated` and `omitted_bytes`. Requests above 256 KiB return
`status: "refused"` with the stream size, requested byte count, and cap; the
node does not silently clamp the request.
Fetch reads the existing retained body. It does not change the success/failure
count or age retention.

## Request and result flow

The worker writes a request JSON file atomically, validates and submits it,
then waits for the result file. The controller daemon processes independent
requests concurrently up to the sum of configured per-box token caps; node
admission applies the shared load and token limits. With `admission: "box"`,
the daemon checkpoints each submitted arm in its request file, keeps the
request queued while the node ticket waits or runs, and publishes the result
only after collection succeeds.

```text
worker -> requests/<request_id>.json -> laptop daemon
                                      |
                                      v
                         concurrent daemon workers
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
  "command_id": "goalflight-full-gate",
  "command": ["./tests/run.sh"],
  "base_sha": "<optional-base-commit-sha>",
  "repo_root": "<absolute-worker-worktree>",
  "cwd_relative": "tests",
  "caller_env": {"CI": "1", "TZ": "UTC"}
}
```

The laptop daemon adds `HEAD`, snapshot SHA, dirty state, selected candidate test
files for a base overlay, and the bundle path after it snapshots the tree.

`kind: targeted` runs only the candidate arm. A `kind: gate` request without a
command runs the configured matched pair; a command request normally runs one
candidate arm, while `--pair` sends the command together with `base_sha` and
routes it through `run_matched_pair`. The result is a JSON file with schema
`goalflight.remote-ci.result.v1`, `status` (`GREEN`, `RED`, `ERROR`, or
`LOCAL_FALLBACK_REQUIRED`), both arm records, sorted new/fixed node ids, and
measured hostnames. Each completed arm includes its `bundle_bytes` and receipt
with parsed pass/fail/skip counts. The wrapper returns the command's exit
status for a verified remote verdict, or the local command's exit status after
fallback.

The wrapper and remote command run in bounded sessions. In blocking mode, an
ambiguous remote outcome resolves locally. In box mode, the wrapper waits across
host sleep using awake monotonic deadlines. If the wrapper is withdrawn or
gives up, it marks the spool request cancelled and the daemon calls node
`cancel`, releasing the ticket's basis hold. A daemon restart re-adopts queued,
running, or finished-uncollected tickets from durable request state. Resident
tickets are not reaped as orphaned runs.

### Resident box admission

The literal config value `"admission": "box"` enables resident admission; the
existing object value keeps blocking admission. The laptop selects a healthy
box, reads its confirmed bundle basis, builds an incremental candidate bundle,
and streams it through the existing node `submit` operation. The node verifies
and stores the bundle before publishing its numbered ticket and basis hold.
The daemon records the selected box, owner, basis, bundle path, and ticket key
in the request spool. After a restart, it retries an ambiguous submit
idempotently or polls the stored ticket key. Transport errors are non-terminal.
Laptop and node ticket deadlines use awake monotonic time, so sleep does not
consume the wait budget.

For ordinary targeted and matched-pair requests, the daemon pins the requested
commit SHAs from the project checkout and builds the same resident bundles;
matched arms keep separate bundles for their base and candidate commits.

After each submit or non-terminal status poll, the daemon returns an internal
`SUBMITTED` state to its scheduler without publishing a result file. It polls
again after `daemon.poll_seconds`, allowing other queued requests to be
submitted. A terminal receipt and bounded stdout/stderr tails are fetched from
the node's retained result body before the final result is written once.

## Node admission and recovery

Every project sharing a physical box uses the same node managed root and
P-core/token defaults. Under `<managed-root>/admission/`, one numbered ticket
queue orders all projects. One node flock token is held for each active run.
Admission is allowed while either measured CPU use is below 95% or load1 is
below twice the physical core count, provided a token is free and the host
memory/CPU floor does not refuse capacity. A box is full only when CPU use is
at least 95% and load1 is at least twice the physical core count. This is the
owner rule (2026-10-19). The checked-in 28-core Studio config sets each token
cap to 28; project configs may set a different cap. Box choice ranks
eligible measured headroom and rotates ties across boxes. The configured server is verified during
placement. Pinned connections preserve verification of the configured server;
verification failures refuse the run. Admitted runs keep using the server that
was verified at admission.
Receipts retain the expected and measured hostnames. On completion, the daemon
verifies that the status sample and receipt agree on the measured hostname, then
retains the receipt if that host differs from the expected one.
The receipt records `box_id` and `expected_hostname` for the admitted placement
and `measured_hostname` for the host that ran it. Python, Node, and Git
are checked before admission; a missing tool makes the box ineligible.
Command requests bootstrap a cached venv from `test_requirements`, hashing
recursively included `-r`/`--requirement` and `-c`/`--constraint` files plus
interpreter identity. Each box and repo has one bare mirror under
`<managed-root>/repos/<repo>/mirror.git`, immutable base trees under
`bases/<commit>`, and an atomic `bases/current` commit pointer. A seat is
re-created as a same-volume APFS clone from its selected base. Its checkout
gets its own Git metadata and index, with object alternates to the mirror; a
clone failure is a typed setup failure and never falls back to a full copy.
Active leases pin their base generations and any bundle prerequisite commits.
The next run re-clones its seat from the base, so prior ignored files and
outputs do not survive reuse.

The remote watcher uses three bounded phases. Setup has a 300-second budget,
execution uses `runner.timeout_seconds`, and finalization has a separate
300-second budget to collect the node's result after execution or cancellation.
The node starts its 300-second setup budget after receiving the command. This is
separate from the workload timeout, so a short workload timeout cannot cut off
provisioning and a long workload does not extend provisioning indefinitely. The
controller gives local snapshot creation a separate 120-second budget, then
starts the remote admission wait after the snapshot is ready. A request already
stale in the local daemon queue is still refused before snapshotting; snapshot
time does not consume the Studio admission budget. Basis lookup, bundle creation,
and bundle transfer each use their own bounded operation budget. The
shared setup budget covers pool-lock wait, mirror fetch, base materialization,
seat clone and replacement cleanup, and lease-safe GC. Setup subprocesses stop
at the same deadline; recursive removal and GC check it while processing
entries. Expiry returns `status: setup-failed` with
`setup_failure_type: setup-timeout` and `command_started: false`, including
when the pool lock remains held.

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
PID. A stale owner cannot cancel a lease owned by a different controller.
Reaping also requires proof that the
owning controller is dead before it cancels a live holder; liveness from
another controller host or an unavailable probe is unknown and cannot
authorize that cancellation. A dead holder is reaped immediately: its
launchd job is removed and its tree is killed. The token stays until that
tree is proved dead. A cwd snapshot that misses the occupant, or that cannot
prove a matching pid is gone, keeps capacity. A launch intent is durable before submit. The workload wrapper's first act
is to record its pid, start token, and coalition. An intent whose job is
not listed is released only when that incarnation is dead and its coalition
has no members. A missing identity is unknown: it is held for a bounded
window from the intent time, then still kept and marked unproven. An empty
cwd is not that proof. A launchctl or cwd command that times out is unknown
and keeps capacity. Past
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
without sweeping the session, so it is left held.

Each repo's seats are `repos/<repo>/seats/s-01`…`s-N`, not a checkout per SHA
and not under `$HOME`. The controller passes `repo` from its config (omitted
means `default`). Slot bookkeeping remains in `repos/<repo>/slot-meta/`, outside
the checkout. A seat is re-created from the current immutable base before each
run. The result index and its directory are fsync'd on every append, before a
body can be deleted. Released run bodies are capped by age and count;
`results/index.jsonl` keeps one line each. Token sentinel files are not deleted.
A conflicting cap still refuses enqueue; list and reap do not, so a corrected
config can recover.

A finished run names one of four outcomes in `result.json` `status`. They are not interchangeable, and a capacity refusal is never a dead workload:

| Outcome | `status` | exit | Meaning |
| --- | --- | --- | --- |
| Refused to start | `capacity-refused` | 75 | Retryable. Memory or CPU floor, or the pool cannot start the job. Not death. Exit 77 is not used. |
| Workload died | `died` | the process exit, or 2 when the process never produced one | A verified command exit is a RED verdict. If the exit is unknown or the box is unverified, the wrapper falls back locally. |
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
