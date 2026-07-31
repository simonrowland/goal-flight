# MEMO → goal-flight controller — cursor-kimi ACP dispatch failures (field report, 2026-07-31)

From: battery-tool-v2 bugs-lane controller (b-855 session).
Context: four consecutive failed attempts to run a **read-only design review** through
`--agent cursor --model kimi-k3-high` (the recommended cursor-kimi3 recipe — NOT the deprecated
kimi-vendor CLI). The engine itself is fine when it runs; every failure was in the dispatch/permission
layer. Owner confirms cursor-kimi3 should work, so these are worth fixing rather than routing around.

## The four failures, in order

| # | dispatch_id | flags | terminal state | notes |
|---|---|---|---|---|
| 1 | `cursor-5353-1785473359` | `--read-only` | **`blocked_os_sandbox`** | died immediately; `--os-sandbox` profiles are documented as bash-shape-only, but `--read-only` ("Equivalent to --os-sandbox read-only") is offered for ACP shapes anyway and hard-blocks them |
| 2 | `cursor-5932-1785473393` | `--os-sandbox off` | **`awaiting_user_confirm`** (wedged) | ACP elicitation nobody can answer in a detached dispatch; sat wedged until killed |
| 3 | `cursor-18700-1785479879` | `--os-sandbox off` | **`awaiting_user_confirm`** (wedged) | same wedge; its own tail even warned "cursor/grok auto-mode does NOT enforce a write boundary … writes are not routed through the ACP permission gate" |
| 4 | `cursor-48344-1785480300` | `--permission-mode auto`, throwaway detached worktree | **`blocked_permission_denied`** after 693 s | reason `requested_action_denied_safe_work_preserved`, `question_ids: ["…-q1"]` — reached `running`, did ~11 min of work, then a permission question was auto-DENIED and the dispatch died |

Meanwhile `--agent grok-code` (bash shape) ran the identical brief files from the identical worktrees
repeatedly without issue, and Claude-side subagents covered the reviews. So this is specific to the
**ACP shape's permission plumbing**, not to the prompts, the worktrees, or the model.

## Diagnosis, as it looks from the field

1. **ACP permission questions have no viable answerer in detached dispatches.** Interactive-mode
   elicitation wedges forever (`awaiting_user_confirm`, #2/#3); `--permission-mode auto` answers, but
   answered **deny** on a question that a *read-only review* needed (#4). Either the auto-answerer is
   deny-by-default, or the question was one a reviewer legitimately needs granted (likely a file read
   or a `git diff` invocation).
2. **`--read-only` is a trap for ACP shapes** (#1): the help text says it equals
   `--os-sandbox read-only`, the sandbox is bash-only, and the combination hard-blocks rather than
   degrading to "no sandbox + advisory read-only prompt clause."
3. **No write boundary in cursor/grok auto-mode** (#3's tail warning) — combined with (2), there is
   currently **no safe way to run a cursor-kimi reviewer**: the sandboxed route blocks, the
   unsandboxed route wedges or gets auto-denied, and if it *had* run, nothing enforces read-only.

## What would fix it (suggestions, in increasing order of effort)

1. **Reject early with a clear error** when `--read-only`/`--os-sandbox` is passed to an ACP-shape
   agent, pointing at the working alternative — instead of launching a worker that instantly blocks.
2. **Log the full question text** whenever `--permission-mode auto` denies (status.json carries only
   `question_ids`). Without the text, a controller cannot tell a deny-by-default bug from a genuinely
   dangerous request, and cannot pre-authorize it next time.
3. **An allowlist knob for auto mode** (e.g. `--permission-auto-allow read,glob,grep,git-diff` or a
   config-file equivalent) so read-only review dispatches can auto-grant the harmless class that
   reviews actually use.
4. **A real read-only enforcement for ACP shapes** (route writes through the permission gate or chroot
   the worker), so `--read-only` can mean something there.

## Repro

```
python3 ~/Repos/goal-flight/scripts/goalflight_dispatch.py \
  --agent cursor --model kimi-k3-high --cwd <any worktree> \
  --prompt-file <any brief> --permission-mode auto --ignore-git-warn --submit
# watch /tmp/goal-flight-501/dispatch/<id>.status.json reach blocked_permission_denied
# (or omit --permission-mode and watch it wedge at awaiting_user_confirm)
```

Status JSONs and steer mailboxes for all four dispatch_ids above were under
`/tmp/goal-flight-501/dispatch/` at the time of writing (tmp-reaper caveat applies; ids and states
are reproduced fully in this memo for when those files age out).


## Addendum (same day): a fifth, unrelated dispatch-layer trap — bash shape this time

`--read-only` (bash-shape grok-code) **cannot be enforced from a worktree under `/tmp`**:
`os sandbox cannot enforce workspace boundaries when cwd is inside allowed temp root '/tmp'`.
The error message is good (states cause + both remedies), but the combination is unfortunate in
practice: goal-flight's own conventions put throwaway worktrees under `/private/tmp/…`, so the
recommended read-only review posture silently conflicts with the recommended worktree location.
Suggest either documenting a blessed non-tmp scratch root for review worktrees, or letting the
sandbox treat an explicitly-passed `--cwd` under /tmp as the workspace boundary itself.
