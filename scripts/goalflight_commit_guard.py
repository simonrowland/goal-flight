#!/usr/bin/env python3
"""goal-flight commit guard.

Prevents the parallel-dispatch commit collision: when one or more
goal-flight workers have active leases against the current project_root,
and the controller runs bare `git commit` (no pathspecs), the commit
silently bundles any staged WIP from other workers into a commit owned
by the controller (or by whichever worker fires `git commit` first).
Concrete failure observed 2026-05-27 in commit fb05e84 where the
controller's `git commit` bundled chunks 6/7 partial WIP.

This guard is invoked as a git pre-commit hook (install via
`scripts/install-commit-guard.sh` or symlink directly into
`.git/hooks/pre-commit`).

Decision logic:

1. Read `scripts/goalflight_capacity.py status --json`. Filter active
   leases whose `project_root` matches the current repo root.
2. If no same-root active leases → exit 0 (no guard needed).
3. If same-root active leases exist AND the commit has no explicit
   pathspecs (detected via GIT_PARTIAL_COMMIT or `git diff --cached`
   matching all staged files) → REFUSE with an actionable error message.
4. Override path: `GOALFLIGHT_COMMIT_GUARD_OVERRIDE=<lease-id>[,...]`
   in env, or `--override-active-leases <ids>` flag passed via the
   wrapper. Refusal message names the variable and shows the lease IDs.

Refusal exit code: 2 (distinct from misuse / config errors at 1).

The guard is intentionally a runtime gate, not a doc rule — the
discipline ("commit your scope with `git commit -- <files>`, never
bare `git commit`") is hard to enforce as a textual invariant because
the wrong action happens silently with no prompt. The teaching is
the error message at failure time. See
`feedback_prefer_docs_and_tests_over_guard_scripts` memory.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT_FALLBACK = SCRIPT_DIR.parent


def _git_repo_root() -> Path:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return REPO_ROOT_FALLBACK
    return Path(out.stdout.strip())


def _capacity_status(repo_root: Path) -> dict:
    capacity = SCRIPT_DIR / "goalflight_capacity.py"
    if not capacity.exists():
        return {}
    try:
        out = subprocess.run(
            ["python3", str(capacity), "status", "--json"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=5,
        )
        if out.returncode != 0:
            return {}
        return json.loads(out.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return {}


def _active_same_root_leases(repo_root: Path) -> list[dict]:
    data = _capacity_status(repo_root)
    # `goalflight_capacity.py status --json` returns `{"active": [...]}` —
    # already filtered to state=active. Keep a defensive state check anyway.
    active = data.get("active") or []
    target = str(repo_root.resolve())
    matched: list[dict] = []
    for lease in active:
        if lease.get("state") and lease.get("state") != "active":
            continue
        lp = lease.get("project_root")
        if not lp:
            continue
        try:
            if str(Path(lp).resolve()) == target:
                matched.append(lease)
        except OSError:
            continue
    return matched


def _commit_is_partial() -> bool:
    """Best-effort detection of whether the in-flight `git commit` was
    invoked with explicit pathspecs.

    git sets GIT_INDEX_FILE in some hook scenarios but not reliably for
    pre-commit. We use the presence of `GIT_PARTIAL_COMMIT` if available
    (set internally for `git commit -- <pathspec>`), and fall back to
    checking whether the working-tree differs from index for any file
    that's staged (the partial-commit indicator most hooks rely on).
    """
    if os.environ.get("GIT_PARTIAL_COMMIT"):
        return True
    # When the user does `git commit -- <pathspec>`, git constructs the
    # commit from the working tree of the named paths (auto-staging them
    # from working tree), and leaves other staged paths alone. The pre-
    # commit hook fires AFTER that staging happens for the partial commit.
    # We can't directly observe the user's argv from a pre-commit hook,
    # but the canonical detection is: any staged file whose working-tree
    # state differs from the staged version indicates the user's pre-
    # existing staging is still there (i.e., the commit IS partial).
    try:
        out = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True, text=True, check=True,
        )
        staged = [p for p in out.stdout.splitlines() if p]
        wt = subprocess.run(
            ["git", "diff", "--name-only"],
            capture_output=True, text=True, check=True,
        )
        unstaged = {p for p in wt.stdout.splitlines() if p}
    except subprocess.SubprocessError:
        return False
    # If the user did `git commit -- <pathspecs>`, the unstaged set will
    # include files that were staged but not in the pathspec list. That
    # signature is "some staged paths but the working tree still has
    # un-staged changes to those same files" — a strong heuristic the
    # commit is partial. Pure conservative behavior on uncertainty: treat
    # as NOT partial (= apply the guard).
    overlap = set(staged) & unstaged
    return bool(overlap)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="goal-flight commit guard")
    parser.add_argument(
        "--override-active-leases",
        default="",
        help="Comma-separated lease IDs (or 'all') to bypass the guard.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    repo_root = _git_repo_root()
    leases = _active_same_root_leases(repo_root)

    if not leases:
        if args.json:
            print(json.dumps({"ok": True, "leases": []}))
        return 0

    override_env = os.environ.get("GOALFLIGHT_COMMIT_GUARD_OVERRIDE", "")
    override_flag = args.override_active_leases
    override_raw = ",".join(p for p in (override_env, override_flag) if p)
    override_ids = {x.strip() for x in override_raw.split(",") if x.strip()}
    if "all" in override_ids:
        if args.json:
            print(json.dumps({"ok": True, "override": "all", "leases": leases}))
        return 0
    blocked_leases = [l for l in leases if l.get("dispatch_id") not in override_ids]
    if not blocked_leases:
        if args.json:
            print(json.dumps({"ok": True, "override": list(override_ids), "leases": leases}))
        return 0

    if _commit_is_partial():
        # Partial commit (git commit -- <pathspec>) is exactly the safe
        # shape we want users to use. Let it through.
        if args.json:
            print(json.dumps({"ok": True, "partial": True, "leases": leases}))
        return 0

    # Refusal path with actionable error message.
    lease_listing = "\n".join(
        f"  - {l.get('dispatch_id')} (agent={l.get('agent')}, pid={l.get('worker_pid')})"
        for l in blocked_leases
    )
    dispatch_ids = ",".join(l.get("dispatch_id") or "" for l in blocked_leases)
    msg = (
        "goal-flight commit guard: REFUSED.\n\n"
        f"Active worker dispatches detected against this repo ({len(blocked_leases)}):\n"
        f"{lease_listing}\n\n"
        "Bare `git commit` would bundle their staged WIP into your commit.\n"
        "Fixes (pick one):\n\n"
        "  (a) Commit ONLY your scope with explicit pathspecs:\n"
        "        git commit -m '...' -- <file1> <file2> ...\n"
        "      This commits only the named files; other staged paths stay staged\n"
        "      for the worker(s) to commit themselves.\n\n"
        "  (b) Wait for the active dispatches to land their own commits, then\n"
        "      re-run your commit.\n\n"
        "  (c) Override (only if you know the staged set is yours):\n"
        f"        GOALFLIGHT_COMMIT_GUARD_OVERRIDE={dispatch_ids} git commit ...\n"
        "      Or pass `--override-active-leases all` to the guard.\n\n"
        "See `protocols/dispatched-worker-recovery.md` for the recovery protocol\n"
        "if a worker's commit got bundled into someone else's (the fb05e84 class).\n"
    )
    if args.json:
        print(json.dumps({"ok": False, "leases": blocked_leases, "message": msg}))
    else:
        sys.stderr.write(msg)
    return 2


if __name__ == "__main__":
    sys.exit(main())
