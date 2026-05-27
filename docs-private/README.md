# Private project runtime state

This tree holds goal-flight durable state for a **target repository** (the
project you are running toward a goal), not private notes for the goal-flight
skill repository itself.

Only this README and directory placeholders are tracked in git. Runtime files
created by `init`, decomposition, dispatch, and reviews stay local and are
gitignored.

Run `/goal-flight init <topic>` in a target project to populate files from
`templates/`. See `commands/init.md` and `templates/rag-corpus-schema.md.tpl`
for the expected layout under `rag/`, `reviews/`, and related paths.
