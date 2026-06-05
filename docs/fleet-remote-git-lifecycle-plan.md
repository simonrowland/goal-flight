# Fleet Remote Git Lifecycle Plan

## Problem

Fleet dispatch can start a worker on a remote Mac Studio, create a remote
worktree, run the worker, mirror status, and release account locks. It does not
yet give the controller a low-friction way to collect, review, merge, and push
the worker's git result.

The desired operating model is:

- workers run the normal code-test-review loop
- workers commit only when clean
- controller integrates commits, not raw file trees
- patches exist only as recovery artifacts
- public pushes remain controller-owned and explicitly approved

## Review State

Iteration 1 applied:

- made base sync a named controller-side command instead of implicit behavior
- removed remote push from the default allowlisted command plan
- added direct remote status fallback for stale or quarantined mirror state
- added cleanup coverage for temporary base refs

Iteration 2 applied:

- made collect infer node and repo path from dispatch metadata by default
- required SSH git operations to preserve fleet node SSH details
- added branch/ref validation before any SSH git operation

Iteration 3 applied from autoreview:

- made collected refs immutable unless an explicit recollect flag is passed
- added expected source SHA guards for `sync-base`
- added a path-validated artifact transfer primitive for bundle/patch recovery

Iteration 4 applied from challenge review:

- split collection from landing so review is a required boundary
- added controller-side lifecycle state for collect/review/land/cleanup
- added setup-failure cleanup for dispatches that fail after worktree creation
- made `sync-base` a separate pre-dispatch step, not an implicit dispatch mode
- made recovery artifacts manifest-driven

Iteration 5 applied from autoreview rerun:

- added explicit `--apply` live flags for dry-run command specs
- added `artifacted` to persisted lifecycle state

Iteration 6 applied from final autoreview:

- added failed/dirty lifecycle states and artifact recovery transitions

Iteration 7 applied from convergence autoreview:

- made cleanup preview-only by default in all states
- made persisted lifecycle `state` one current enum value plus history
- added `--apply` to `artifact-get`

Iteration 8 applied from convergence autoreview:

- added binary-safe untracked-file capture for patch recovery
- added explicit remote base-ref cleanup
- strengthened artifact path validation against symlink escapes

Iteration 9 applied from convergence autoreview:

- restricted artifact retrieval to the dedicated artifact directory only
- moved dirty controller worktree guard from old collect modes to `land`

Iteration 10 applied from convergence autoreview:

- removed stale status/worktree artifact retrieval boundary from safety rules

Iteration 11 applied from convergence autoreview:

- constrained `sync-base` destination to derived `gf/base/<dispatch-id>` refs

Still pending before implementation:

- engineering review of command boundaries and failure modes
- security review of SSH git fetch/push ref handling
- live workflow review against `mac-studio-256-1`

## Current State

Remote dispatch currently does enough for worker start:

- resolves a fleet node and SSH alias
- runs `git fetch --quiet origin` on the remote checkout
- creates a remote worktree from `origin/main`
- runs ACP in that remote worktree
- mirrors remote status into the controller fleet register
- reconciles terminal dispatches and billing/account locks

Current gaps:

- base ref is effectively fixed to `origin/main`
- no first-class remote worker branch naming
- no status contract for `base_sha`, `branch`, `head_sha`, and dirty state
- no controller command to fetch a remote worker branch
- no controller command to land or cherry-pick the fetched worker commit
- no recovery path for dirty or partially complete remote worktrees
- no direct-status fallback when fleet mirror state is stale or quarantined
- no controlled GitHub fallback for environments without direct SSH fetch

## Decision

Default to Git over the existing SSH path.

Do not use GitHub worker branches by default. Do not use rsync or file sharing
as the normal path.

Rationale:

- Git preserves commits, renames, deletes, binary changes, ancestry, and
  conflict semantics.
- SSH is already required for remote fleet dispatch.
- Public GitHub worker branches leak WIP, can trigger CI, and require remote
  GitHub auth and cleanup.
- Rsync and shared folders make it easy to overwrite the wrong state and hard
  to reason about deletes, renames, and conflicts.

GitHub worker branches remain an explicit fallback:

```text
--publish-worker-branch
```

The controller still owns final origin pushes.

## Roles

### Worker

The worker owns local iteration inside one remote worktree:

1. edit files
2. run focused tests
3. run review
4. fix accepted findings
5. commit with explicit pathspecs
6. write terminal status with branch and commit metadata

The worker must not push to origin by default.

### Controller

The controller owns integration:

1. verify remote terminal status
2. fetch the remote worker branch over SSH
3. review the fetched diff
4. merge or cherry-pick locally
5. run verification
6. push origin only after explicit user approval
7. clean up remote worktree and branch

## Happy Path

### Dispatch Start

Controller chooses a dispatch id:

```text
acp-<token>
```

Controller prepares a base ref:

- if work is already on origin, use `origin/main` or `origin/<branch>`
- if work is local-only, sync a private controller base ref directly to the
  remote repo over SSH

Local-only base sync is a controller operation:

```text
git push mac-studio-256-1:/Users/simonrowland/Repos/goal-flight \
  HEAD:refs/heads/gf/base/<dispatch-id>
```

This does not publish to GitHub. It writes only to the trusted remote worker
repo over the existing SSH alias. It should be cleaned up after collect.

Remote worktree layout:

```text
~/.goal-flight/worktrees/<dispatch-id>
```

Remote branch:

```text
gf/worker/<dispatch-id>
```

Remote command shape:

```text
git -C <repo_root> fetch --quiet origin
git -C <repo_root> worktree add -b gf/worker/<dispatch-id> <worktree_path> <base_ref>
python3 <repo_root>/scripts/goalflight_acp_run.py ... --cwd <worktree_path>
```

### Worker Completion

Worker terminal status must include:

```json
{
  "dispatch_id": "acp-...",
  "state": "complete",
  "ok": true,
  "base_ref": "origin/main",
  "base_sha": "<sha>",
  "branch": "gf/worker/acp-...",
  "head_sha": "<sha>",
  "dirty": false,
  "tests": [
    {"command": "python3 -m pytest ...", "status": 0}
  ],
  "review": {
    "status": "clean",
    "tool": "autoreview"
  }
}
```

### Controller Collect

Controller fetches directly from the remote repo:

```text
git fetch mac-studio-256-1:/Users/simonrowland/Repos/goal-flight \
  gf/worker/<dispatch-id>:refs/remotes/fleet/mac-studio-256-1/<dispatch-id>/sha/<head-sha>
```

The concrete SSH target must be derived from the fleet node record, preserving
the configured user, host, port, identity file, and repo root. The hardcoded
`mac-studio-256-1:/Users/...` shape above is illustrative only.

Controller validates:

- fetched ref exists
- fetched head matches `head_sha`
- canonical collected ref is absent or already points at the same `head_sha`
- `base_sha` is an ancestor or expected merge base
- remote status says `dirty=false`
- worker tests and review are recorded
- controller lifecycle state has not already landed a different commit

The canonical collected ref:

```text
refs/remotes/fleet/<node>/<dispatch-id>/current
```

The `/current` ref may be created only after the immutable `/sha/<head-sha>`
ref is fetched and validated. It must not be moved to a new object unless the
operator passes an explicit recollect/force flag.

`collect` must not rely only on the fleet mirror. If mirror ingest is stale,
quarantined, or stopped by sequence regression, `collect` should read the
remote dispatch status file directly over SSH before deciding whether a worker
is collectible.

### Controller Land

Landing is separate from collection and requires an explicit reviewed state.

Default landing action:

```text
git cherry-pick <fetched_head>
```

Use merge only when the worker intentionally produced a multi-commit branch:

```text
git merge --no-ff refs/remotes/fleet/<node>/<dispatch-id>/current
```

Landing is refused unless the collected ref has been independently reviewed and
recorded as accepted.

Push remains a separate explicit user-approved operation:

```text
git push origin HEAD:<branch>
```

## Recovery Paths

`fleet collect` should try recovery in this order.

### 1. Fetch Commit

Use when remote status is complete, clean, and committed.

Outcome:

- controller gets a normal git ref
- no patches involved

### 2. Fetch Bundle

Use when direct SSH fetch from the remote repo fails but the remote can still
write files.

Remote:

```text
git -C <worktree_path> bundle create <dispatch-id>.bundle <base_sha>..HEAD
```

Remote writes an artifact manifest next to the bundle:

```json
{
  "dispatch_id": "acp-...",
  "kind": "bundle",
  "path": "<remote-path>",
  "sha256": "<sha>",
  "size": 12345,
  "base_sha": "<sha>",
  "head_sha": "<sha>"
}
```

Controller retrieves the manifest and bundle through `artifact-get`, verifies
size/checksum, then fetches from the bundle.

### 3. Fetch Patch

Use when the worker is dirty or failed before commit.

Remote emits:

- `git status --porcelain=v1`
- `git diff --binary`
- `git diff --cached --binary`
- `git ls-files --others --exclude-standard -z`
- binary-safe archive of untracked regular files
- test/review logs
- suggested commit message
- artifact manifest with path, size, sha256, and dirty-file summary

Controller applies manually or abandons.

### 4. Manual Salvage

Use when git metadata is inconsistent or remote state is suspect.

Controller prints:

- SSH alias
- remote worktree path
- branch
- status file path
- recommended inspection commands

No automatic writes.

## Command Surface

### `fleet dispatch`

Add:

```text
--base-ref <ref>
--base-sha <sha>
--worker-branch <branch>
--sync-base {origin,none}
--publish-worker-branch
```

Defaults:

```text
--base-ref origin/main
--worker-branch gf/worker/<dispatch-id>
--sync-base origin
```

For local-only bases, run `fleet sync-base` first and pass the resulting remote
base ref as `--base-ref`. `dispatch` does not perform SSH-ref sync implicitly.

### `fleet collect`

New command:

```text
python3 scripts/goalflight_fleet.py collect \
  --dispatch-id <id> \
  [--node <node>] \
  --mode fetch-only|bundle|patch \
  --apply \
  --json
```

Default:

```text
--mode fetch-only
```

Default behavior is preview-only. `--apply` is required before `collect` fetches
refs or writes local artifact files.

If `--node` is omitted, `collect` reads the dispatch metadata from the fleet
register and uses the original node and repo root recorded at dispatch time.

`fetch-only` mutates only refs under:

```text
refs/remotes/fleet/<node>/<dispatch-id>/
```

Before fetching, `collect` reads terminal metadata from the direct remote status
path when the controller mirror is unavailable or stale.

### `fleet review-accept`

New command:

```text
python3 scripts/goalflight_fleet.py review-accept \
  --dispatch-id <id> \
  --reviewed-ref refs/remotes/fleet/<node>/<id>/sha/<head-sha> \
  --review-tool autoreview \
  --review-result clean \
  --json
```

This records the review boundary in fleet state. It does not mutate source
files.

### `fleet land`

New command:

```text
python3 scripts/goalflight_fleet.py land \
  --dispatch-id <id> \
  --mode cherry-pick|merge \
  --expected-head-sha <sha> \
  --apply \
  --json
```

Default is dry-run. Live landing requires:

- clean controller worktree
- collected immutable ref exists and matches `--expected-head-sha`
- review acceptance recorded for the same ref
- lifecycle state is not already landed

`land` is still separate from `git push origin`.

### `fleet sync-base`

New command:

```text
python3 scripts/goalflight_fleet.py sync-base \
  --node <node> \
  --dispatch-id <id> \
  --source-ref HEAD \
  --expected-source-sha <sha> \
  --apply \
  --json
```

Default mode is dry-run. Live mode performs the controller-side SSH git push to
the remote worker repo. It must not push to GitHub.

`sync-base` must use the same SSH spec resolution path as dispatch, so custom
user, port, and identity-file settings are preserved.

Before live sync, `sync-base` resolves `--source-ref` and refuses to push unless
it equals `--expected-source-sha`. The preferred call path passes an explicit
commit SHA as `--source-ref` and the same value as `--expected-source-sha`.

The destination ref is not user-selectable. It is derived from the dispatch id:

```text
refs/heads/gf/base/<dispatch-id>
```

### `fleet cleanup`

New command:

```text
python3 scripts/goalflight_fleet.py cleanup \
  --dispatch-id <id> \
  --node <node> \
  --remote-worktree \
  --remote-branch \
  --remote-base-ref \
  --local-ref \
  --apply \
  --json
```

Default is always preview-only. `--apply` is required for any remote worktree,
remote branch, remote base ref, or local ref removal.

### `fleet artifact-get`

New command:

```text
python3 scripts/goalflight_fleet.py artifact-get \
  --dispatch-id <id> \
  --node <node> \
  --remote-path <path> \
  --local-dir docs-private/dispatch-artifacts/<id> \
  --sha256 <expected> \
  --apply \
  --json
```

Use only for bundle and patch recovery artifacts. The remote path must be under
the dedicated dispatch artifact directory, not the general worktree or status
directory. The local destination must be under
`docs-private/dispatch-artifacts/<dispatch-id>/`.
Default is preview-only. `--apply` is required before files are copied locally.

Path validation must resolve the remote realpath before transfer, reject
symlinks, require a regular file, and require the resolved path to remain under
the dispatch artifact directory. A manifest-provided checksum is necessary but
not sufficient because the worker controls the manifest.

### `fleet artifact-create`

New command:

```text
python3 scripts/goalflight_fleet.py artifact-create \
  --dispatch-id <id> \
  --kind bundle|patch \
  --apply \
  --json
```

This asks the remote node to create a bundle or binary patch plus manifest in
the dispatch artifact directory. `artifact-get` retrieves only artifacts listed
in a valid manifest.

## Controller Lifecycle State

Persist controller integration state per dispatch:

```json
{
  "dispatch_id": "acp-...",
  "node": "mac-studio-256-1",
  "remote_repo_root": "/Users/simonrowland/Repos/goal-flight",
  "remote_worktree": "~/.goal-flight/worktrees/acp-...",
  "remote_status_path": "~/.goal-flight/dispatches/acp-.../status.json",
  "base_ref": "origin/main",
  "base_sha": "<sha>",
  "head_sha": "<sha>",
  "immutable_ref": "refs/remotes/fleet/<node>/<id>/sha/<head-sha>",
  "collected_ref": "refs/remotes/fleet/<node>/<id>/current",
  "reviewed_ref": null,
  "landed_sha": null,
  "artifact_sha": null,
  "cleanup_ready": false,
  "state": "dispatched",
  "state_history": [
    "dispatched",
    "complete"
  ]
}
```

`state` is one current enum value. Valid values are:

```text
dispatched, setup_failed, complete, failed, dirty, collected, reviewed, landed,
artifacted, cleaned
```

Allowed transitions:

```text
dispatched -> setup_failed -> cleaned
dispatched -> complete -> collected -> reviewed -> landed -> cleaned
dispatched -> failed -> artifacted -> cleaned
dispatched -> dirty -> artifacted -> cleaned
complete -> dirty -> artifacted -> cleaned
```

Retry and cleanup commands must use this state instead of inferring intent from
status files alone.

## Allowlisted Remote Git Operations

Add narrow command classes, not arbitrary shell:

- `git_rev_parse`
- `git_status_porcelain`
- `git_branch_create`
- `git_fetch_ref`
- `git_bundle_create`
- `git_format_patch_binary`
- `artifact_get`
- `git_worktree_remove`
- `git_branch_delete`
- `remote_identity_probe`

Do not add remote `git push origin` as a default command class.

Do not add a remote push command class in the first implementation. Syncing the
base to the worker repo and fetching the worker branch back are controller-side
SSH git operations. A worker-origin push is only for the explicit GitHub
fallback.

If GitHub branch publication is supported, it must require:

```text
--publish-worker-branch
```

and must emit the exact remote/ref before executing.

## Safety Rules

- No public origin push from a worker unless the user explicitly opted into
  worker-branch publication.
- Controller push to origin remains separate from collect/land.
- `collect` verifies direct remote status when mirror state is stale or
  quarantined.
- `collect` refuses mismatched `head_sha`.
- `collect` fetches first to an immutable `/sha/<head-sha>` local ref and
  refuses to move an existing `/current` collected ref without
  `--force-recollect`.
- `collect` refuses non-ancestor base unless `--allow-diverged-base` is passed.
- remote cleanup refuses uncollected clean commits unless `--force` is passed.
- `sync-base` refuses to overwrite an existing `gf/base/<dispatch-id>` ref
  unless `--force` is passed.
- `sync-base` refuses when `--source-ref` does not resolve to
  `--expected-source-sha`.
- `artifact-get` refuses remote paths outside the dedicated dispatch artifact
  directory and verifies size/checksum before use.
- `artifact-get` must reject symlinks and resolved paths outside the dispatch
  artifact directory.
- `land` refuses without a matching review acceptance record.
- `land` refuses dirty controller worktrees.
- setup failure after remote worktree creation records `setup_failed` and can be
  cleaned without pretending the worker completed.
- failed or dirty workers can transition to `artifacted` before cleanup.
- destructive remote operations first verify expected hostname, repo root,
  worktree path, and status path.
- all remote command argv must keep prompt payloads redacted.
- all branch names must use neutral, non-sensitive dispatch ids.
- all branch/ref names must pass a strict refname validator before any local or
  remote git command is built.
- node ids and user-supplied worker branch names must pass the same strict
  validator before command construction.
- direct SSH git operations must use the fleet node record, not reconstruct a
  host from alias and hostname alone.

## Tests

### Unit Tests

- dispatch preview includes fetch before worktree add
- dispatch preview uses configurable `base_ref`
- worktree add creates `gf/worker/<dispatch-id>`
- worker terminal status validates required git fields
- collect fetches remote branch into `refs/remotes/fleet/...`
- collect uses immutable `/sha/<head-sha>` refs and refuses `/current` ref
  movement without force
- collect falls back to direct remote status when mirror ingest is stale
- collect refuses mismatched `head_sha`
- collect falls back to bundle when direct fetch fails
- collect emits patch artifacts for dirty remote worktree
- collect preview does not write refs without `--apply`
- review-accept records reviewed ref and review result
- land refuses without review-accept for the same immutable ref
- land refuses dirty controller worktrees
- setup failure after worktree add records cleanup-safe state
- failed worker can create recovery artifact and transition to `artifacted`
- dirty complete worker can create recovery artifact and transition to
  `artifacted`
- patch artifact captures untracked regular files in a binary-safe archive
- sync-base dry-run shows exact source and destination refs
- sync-base refuses existing remote base ref without force
- sync-base refuses a moving source ref when expected SHA differs
- artifact-get rejects path traversal and remote paths outside dispatch roots
- artifact-get verifies checksum before reporting success
- artifact-create writes manifest with kind/path/size/sha256/head/base fields
- artifact-get rejects symlink artifacts and verifies resolved remote realpath
- collect infers node/repo from dispatch metadata when `--node` is omitted
- SSH git fetch/push preserves node user, port, and identity file
- invalid branch/ref names are rejected before command construction
- cleanup dry-run lists remote branch, worktree, and local ref
- cleanup supports explicit `--remote-base-ref`
- cleanup refuses uncollected branch by default

### Integration Tests

- stub remote: worker clean commit -> collect fetch-only
- stub remote: worker clean commit -> collect, review-accept, land cherry-pick
- stub remote: dirty worker -> patch artifact
- stub remote: dirty worker with untracked file -> patch artifact includes it
- stub remote: direct fetch failure -> bundle fallback
- stub remote: worktree add succeeds, ACP spawn fails -> setup_failed cleanup
- stub remote: collected ref exists at different SHA -> recollect refused
- stub remote: land before review-accept -> refused
- live Mac Studio smoke: dispatch, remote clean commit, collect fetch-only,
  cleanup dry-run
- live Mac Studio stale-mirror smoke: direct remote status still permits
  collect fetch-only when mirror ingest is quarantined

### Manual Live Test

Use `mac-studio-256-1`:

```text
GOALFLIGHT_LIVE_SSH=1 \
GOALFLIGHT_FLEET_SSH_ALIAS=mac-studio-256-1 \
GOALFLIGHT_FLEET_NODE=mac-studio-256-1 \
python3 scripts/goalflight_fleet.py dispatch ...
```

Then:

```text
python3 scripts/goalflight_fleet.py collect \
  --dispatch-id <id> \
  --mode fetch-only \
  --apply \
  --json
```

Verify:

- remote hostname is `Mac-Studio-256-1`
- remote branch exists
- local fetched ref matches remote `head_sha`
- controller worktree remains clean after fetch-only

## Implementation Chunks

### Chunk 1: Status Contract

- add git metadata fields to terminal status
- validate clean/dirty state
- tests for status schema and dirty state detection

### Chunk 2: Worker Branch Creation

- add configurable `base_ref`
- create named worker branch in remote worktree
- preserve current `origin/main` default
- tests for command order and branch naming

### Chunk 3: Base Sync

- add `fleet sync-base`
- controller-side SSH git push to remote worker repo
- dry-run by default
- refuse overwrite without force
- require `--expected-source-sha`

### Chunk 4: Collect Fetch-Only

- add `fleet collect --mode fetch-only`
- direct SSH git fetch from remote repo
- validate `head_sha` and base ancestry
- fetch to immutable `/sha/<head-sha>` ref first
- refuse `/current` collected ref replacement without force
- preview by default, `--apply` to write refs
- direct remote status fallback when mirror is stale
- no controller tree mutation

### Chunk 5: Review Boundary

- add `fleet review-accept`
- record reviewed ref and result
- refuse mismatched reviewed refs

### Chunk 6: Land Modes

- add `fleet land`
- add `land --mode cherry-pick`
- add `land --mode merge`
- require clean controller tree
- require review acceptance
- tests for conflict/refusal behavior

### Chunk 7: Recovery Artifacts

- add `fleet artifact-create`
- add `fleet artifact-get`
- add bundle fallback
- add binary patch fallback
- record artifacts under `docs-private/dispatch-artifacts/<dispatch-id>/`
- path and checksum validation for copied artifacts

### Chunk 8: Setup-Failure Cleanup

- record setup failure after remote worktree creation
- cleanup stale setup-failed worktrees and branches safely
- retry same dispatch id only after cleanup or explicit new id

### Chunk 9: Cleanup

- remove remote worktree
- delete remote worker branch
- delete remote base branch
- remove local tracking ref
- dry-run by default

### Chunk 10: Live Smoke

- add manual live smoke covering dispatch -> collect fetch-only -> cleanup dry-run
- document Mac Studio operator flow

## Review Questions

1. Should `collect` ever push a worker branch to GitHub automatically?
   Recommendation: no. Make it explicit fallback only.
2. Should default landing be cherry-pick or merge?
   Recommendation: cherry-pick for one-commit chunks, merge only for declared
   multi-commit worker output.
3. Should controller sync local-only base work by SSH ref or temporary GitHub
   branch?
   Recommendation: SSH ref first, GitHub branch only when direct SSH cannot be
   used.
4. Should patches be stored in git-visible docs?
   Recommendation: no. Store recovery artifacts under `docs-private/`.

## Acceptance Criteria

- A worker on `mac-studio-256-1` can produce a clean commit in a remote worktree.
- Controller can fetch that commit without public GitHub publication.
- Controller can review and land the commit locally.
- Controller can push origin only after explicit approval.
- Dirty or failed worker output is recoverable as patch/bundle artifacts.
- Cleanup is safe and dry-run first.
