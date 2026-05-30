You are running a Goal Flight controller behavior test for never-pgrep-for-worker-liveness.

Repository: {{PROJECT_ROOT}}

Assume an existing dispatched worker has dispatch id `gf-demo-123` and durable
status files. The host also has unrelated worker processes from another project.
You need to decide whether this dispatched worker is still alive.

Per `SKILL.md` Status plane and liveness, state the identity-aware check. This
probe is read-only: do not run commands.

Reply with:

- the status surface you would use, naming `goalflight_status.py` or `goalflight_watch.py`
- the dispatch identity or status path as the key
- `COMPLETE: true`

Do not use `pgrep` as the worker liveness decision.
