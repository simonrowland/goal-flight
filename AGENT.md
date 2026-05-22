# Agent Notes

## Git-Visible Trigger Hygiene

- Never put the known billing-trigger harness codenames from this thread in
  git-visible metadata: filenames, directories, branch names, tag names, commit
  messages, generated JSON manifest names, or installer output paths.
- Do not repeat those exact codenames in agent instruction files loaded by local
  coding tools.
- Installer aliases must not become manifest filenames. If an installer command
  receives `--agent=<trigger-codename>`, map that alias to a neutral manifest
  filename before reading or writing `adapters/*.json`.
- When porting legacy packages, rename codename JSON manifests before staging,
  then run the trigger audit against status, paths, commit messages, and history.
