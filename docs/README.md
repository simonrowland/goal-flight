# goal-flight documentation

Public documentation for the goal-flight skill repository. Host install
artifacts live under `configs/<host>/` and are written to user or project paths
by `./install.sh` / `./setup.sh` — they are not checked in at the repository
root.

| Document | Purpose |
| --- | --- |
| [architecture.md](architecture.md) | Portable core, runtime scripts, capacity, and validation boundaries |
| [fleet.md](fleet.md) | Multi-node SSH fleet: bootstrap, dispatch, watch, reconcile |
| [hosts/cursor.md](hosts/cursor.md) | Cursor install, MCP, and project-local setup |
| [hosts/opencode.md](hosts/opencode.md) | OpenCode install, skills, MCP, and bash-tail workers |

Entry surfaces outside this folder: `README.md`, `SKILL.md`, `CHANGELOG.md`, and
`CONTRIBUTING.md`.
