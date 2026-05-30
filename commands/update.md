# /goal-flight update

Refresh **goal-flight itself** + the worker CLIs it dispatches to. Two
sweeps in one command: pull latest goal-flight from origin, then run each
CLI's built-in update mechanism. Reports a diff table for both.

Useful before starting a long unattended run (you want both fresh skill
rules and fresh workers) or when triaging worker-side flakiness (might
already be fixed upstream).

## Installed skill resync

After source `SKILL.md`, `commands/`, `protocols/`, `templates/`, or `adapters/`
changes, copied host installs need `./install.sh <host>` from the source repo
unless the host skill path is a symlink; doctor JSON reports
`installed_skill_drift`, and text mode prints `installed_skill_md_hash` WARNs.

## Recipe

### Sweep 1 — Update goal-flight plugin

1. Resolve the install path. The controller already knows its own
   `<skill-root>` — pass that as `GFROOT`. If `<skill-root>` is a symlink
   (the common install pattern), resolve it to the underlying source
   repo so `git` operates on the canonical checkout:

   ```bash
   GFROOT="$(realpath <skill-root>)"
   ```

   `realpath` is BSD-native on macOS and GNU-native on Linux — same
   flag-less behavior on both. If `$GFROOT` isn't a git repo (e.g., the
   user installed via a tarball or a marketplace bundle that doesn't
   expose `.git`), emit
   `STATUS: plugin install path is not a git checkout (path=$GFROOT) — skipping plugin update` and skip to Sweep 2.

2. Capture current state:

   ```bash
   GF_BEFORE_HEAD="$(git -C "$GFROOT" rev-parse --short HEAD)"
   GF_BEFORE_VER="$(cat "$GFROOT/VERSION" 2>/dev/null || jq -r .version "$GFROOT/.claude-plugin/plugin.json" 2>/dev/null)"
   ```

3. Refuse to pull if working tree is dirty (don't risk losing the user's
   in-flight changes). `git diff-index` is blind to untracked files; use
   `git status --porcelain` which catches modified, staged, AND untracked
   entries:

   ```bash
   if [ -n "$(git -C "$GFROOT" -c core.autocrlf=false status --porcelain)" ]; then
     # dirty — skip plugin update
     emit "STATUS: goal-flight working tree has uncommitted changes — skipping plugin update; resolve dirty state first."
     # ...then fall through to Sweep 2 anyway; CLI updates don't touch the source repo.
   fi
   ```

   The porcelain check returns a non-empty string for any of: modified
   tracked files, staged changes, untracked files, conflicts. All four
   should block the pull.

4. Default update is fast-forward-only-or-skip. This is the automatic path:

   ```bash
   "$GFROOT/scripts/goalflight_autoupdate.py" --repo "$GFROOT" --json
   ```

   The helper refuses dirty trees, uses `git -c core.autocrlf=false` on
   Windows so CRLF conversion does not manufacture dirtiness, and only runs
   `merge --ff-only` when the local checkout is strictly behind `origin/main`.
   Ahead or diverged checkouts emit `skipped_ahead` / `skipped_diverged` and
   leave the working tree untouched. On native Windows it also caches the
   result once per controller session to avoid repeated fetch latency.

   `pull --rebase` is deliberate, user-invoked repair for a diverged checkout:

   ```bash
   "$GFROOT/scripts/goalflight_autoupdate.py" --repo "$GFROOT" --rebase --json
   ```

   If the rebase path reports conflicts, emit
   `BLOCKED: goal-flight rebase conflict — manual resolution required at $GFROOT` and surface to user. Do NOT continue to Sweep 2 (a half-updated plugin is worse than no update).

5. Capture new state:

   ```bash
   GF_AFTER_HEAD="$(git -C "$GFROOT" rev-parse --short HEAD)"
   GF_AFTER_VER="$(cat "$GFROOT/VERSION" 2>/dev/null || jq -r .version "$GFROOT/.claude-plugin/plugin.json" 2>/dev/null)"
   ```

6. Re-run the test suite on the new HEAD:

   ```bash
   bash "$GFROOT/tests/run.sh"
   ```

   If tests fail on the freshly pulled head, surface as
   `BLOCKED: goal-flight tests fail on new HEAD ($GF_AFTER_HEAD) — consider git -C "$GFROOT" reset --hard $GF_BEFORE_HEAD to revert.` Don't auto-revert; user makes the call.

7. If the version changed, note that the running session is still on the
   OLD skill rules. Surface:
   `STATUS: goal-flight updated to $GF_AFTER_VER ($GF_AFTER_HEAD). Re-run /goal-flight to load the new skill in this session, or start a fresh session.`

### Sweep 2 — Update worker CLIs

1. Probe present-set:

   ```bash
   PY="${GOALFLIGHT_PYTHON:-$(command -v "python${GOALFLIGHT_PYTHON_MAJOR:-3}" || command -v python)}"
   "$PY" "$GFROOT/scripts/goalflight_doctor.py" --project-root "$PWD" --json
   ```

   The doctor returns presence in two places: `capacity.tools` is a flat
   `{<cli>: <bool>}` map covering codex / grok / claude / cursor-agent /
   claude-code-cli-acp / codex-acp. `acp.<adapter>` carries ACP-adapter
   presence and (for some) a version string. Use `capacity.tools` as the
   present-set filter; skip missing CLIs silently. Versions are captured
   separately in step 2 via each CLI's `--version`.

2. For each present CLI, capture current version. Run in parallel via
   background Bash calls; each is sub-second:

   ```bash
   codex --version
   grok --version
   cursor-agent --version
   claude --version
   claude-code-cli-acp --version 2>&1 || npm list -g claude-code-cli-acp --depth=0
   ```

3. Run each CLI's update command. Background-dispatch each — grok is
   ~5s, codex / cursor-agent are ~10-30s, claude can be 60s+ on a fresh
   build, claude-code-cli-acp depends on npm registry speed.

   | CLI | Update command |
   |---|---|
   | codex | `codex update` |
   | grok | `grok update` (or `grok update --version <ver>` for a pin; `--check` for dry-run) |
   | cursor-agent | `cursor-agent update` |
   | claude | `claude update` |
   | claude-code-cli-acp | `npm update -g claude-code-cli-acp` |

4. After each update completes, re-capture the version.

5. Re-run `goalflight_doctor.py --json` and verify the new versions
   probe cleanly (e.g., grok daily-alpha sometimes ships a broken build —
   if `--version` now fails or ACP probe fails, flag in the report).

### Combined report

```
=== goal-flight plugin ===
HEAD:    <before-sha>   →   <after-sha>     (<status>)
VERSION: <before-ver>   →   <after-ver>     (<status>)
tests:   <pass/fail count>

=== worker CLIs ===
CLI                    BEFORE              AFTER               STATUS
codex                  0.130.0             0.131.0             updated
grok                   0.1.213-alpha.1     0.1.218-alpha.1     updated
cursor-agent           2026.05.16-...      2026.05.18-...      updated
claude                 2.1.142             2.1.143             updated
claude-code-cli-acp    <ver>               <ver>               unchanged
```

Statuses per CLI: `updated` (version changed), `unchanged` (no new
version), `failed` (update command returned non-zero — capture stderr in
the report), `skipped` (CLI not present).

Plugin statuses: `updated` (HEAD or version changed), `unchanged` (no new
commits), `skipped-dirty` (working tree had uncommitted changes),
`skipped-non-git` (install path isn't a git checkout),
`blocked-conflict` (rebase failed), `blocked-tests` (tests failed on new
HEAD).

## Notes

- **Don't abort on a single CLI failure.** A failed grok update shouldn't
  block a fresh codex from landing.
- **Network reachability**: each update step needs network. Each updater
  will surface its own connectivity error if offline — no preflight probe
  required. If you want one anyway, `ping -c 1 -W 2 1.1.1.1`,
  `nc -z -w 2 1.1.1.1 443`, or `curl -s -o /dev/null -w '%{http_code}' https://1.1.1.1`
  all work depending on what's installed.
- **Don't run during an active dispatch**: a `codex exec` worker running
  during a `codex update` might end up with mixed binaries. Drain
  in-flight dispatches first (check
  `python3 "$GFROOT/scripts/goalflight_status.py" --json`) or queue the
  update for after.
- **Plugin update is hot-swap-unsafe for the live session.** Loaded
  `SKILL.md` + protocols are cached in this conversation. After Sweep 1
  bumps the skill, the most reliable way to pick up the new rules is a
  fresh session. Re-invoking `/goal-flight` in the live session may
  re-print the skill content but loader behavior across the various
  protocol files is not guaranteed; a clean start is the only sure
  reload path.

## Output convention

Markers per `protocols/worker-markers.md`:

- `STATUS: <sweep> <phase>` — e.g., `STATUS: sweep-1 fetching origin`,
  `STATUS: sweep-2 updating <N> CLIs in parallel`.
- `RESULT: <subject> <before> → <after> (<status>)` — one line per
  item (plugin HEAD, plugin VERSION, each CLI).
- `COMPLETE: plugin <status>; CLIs updated <N>, unchanged <M>, failed <K>, skipped <S>`.
- `BLOCKED:` only on rebase conflicts, test failures on new HEAD, or no
  network at all.
