# Memory Contract

Goal Flight memory is repo-canonical by default.

`memory_backend.canonical` must be `repo_files`. Adapter manifests may describe
host-native memory, Kanban, chat, or plugin state only as a mirror, advisory
source, or projection unless an explicit migration lock allows writeback.

Required fields:

- `repo_files`: canonical repo files the adapter may read or update.
- `host_native.role`: `none`, `mirror`, `advisory`, or `projection`.
- `host_native.backend`: host memory backend name or `null`.
- `mirror_writeback`: whether host memory may write back to repo files.
- `source_epoch` and `snapshot_id`: optional drift-tracking identifiers.
- `migration_lock_required_for_writeback`: writeback lock requirement.
- `drift_policy`: how to resolve divergence; v1 uses `warn_and_prefer_repo`.

Host memory cannot become recovery authority by existing. If host state and
repo state disagree, repo files win unless a migration lock explicitly changes
that rule.

`permission_surface.memory_writes` separately declares whether the adapter may
write memory at all and which backend receives writes. That permission field
does not change canonical ownership.
