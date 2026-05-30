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
sys.path.insert(0, str(SCRIPT_DIR))

import goalflight_compat  # noqa: E402


def _git_repo_root() -> Path:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return REPO_ROOT_FALLBACK
    return Path(out.stdout.strip())


def _capacity_status(repo_root: Path) -> dict | None:
    """Return capacity status JSON or None on failure. None signals
    "status unavailable" — caller decides fail-open vs fail-closed.
    """
    capacity = SCRIPT_DIR / "goalflight_capacity.py"
    if not capacity.exists():
        return None
    try:
        out = subprocess.run(
            [goalflight_compat.python_executable(), str(capacity), "status", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(repo_root),
            timeout=5,
        )
        if out.returncode != 0:
            return None
        return json.loads(out.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None


def _active_same_root_leases(repo_root: Path) -> tuple[list[dict] | None, str | None]:
    """Return (leases, error) — leases is None when capacity status is
    unavailable, signaling the caller should fail-closed (or fail-open
    if the operator opted in).
    """
    data = _capacity_status(repo_root)
    if data is None:
        return None, "capacity status unavailable"
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
    return matched, None


def _commit_is_partial() -> bool:
    """Detect whether the in-flight `git commit` was invoked with explicit
    pathspecs (partial commit), so the guard knows to let it through.

    Canonical signal (per git internals): when `git commit -- <pathspec>`
    runs, git creates a temporary index file at `.git/next-index-<pid>`
    and exports `GIT_INDEX_FILE` pointing at it to the pre-commit hook.
    Bare `git commit` does NOT set this variable (the hook reads the main
    .git/index). This is the strongest signal available from inside a
    pre-commit hook — used by sweep A P1 / D1 reviews to replace the
    heuristic that falsely refused safe pathspec commits.

    Fallbacks (in priority order):
      1. GIT_INDEX_FILE basename starts with "next-index-" → partial.
      2. GIT_PARTIAL_COMMIT env var set (some git versions) → partial.
      3. Otherwise: NOT partial. Conservative — guard applies.

    The previous overlap-heuristic (stage ∩ working-tree-diff) was both
    false-positive-prone (overlap can exist for legit reasons) AND
    false-negative-prone (`git commit -- file` leaves no overlap when
    file2 has identical staged+working-tree state). Replaced entirely.
    """
    if os.environ.get("GIT_PARTIAL_COMMIT"):
        return True
    git_index_file = os.environ.get("GIT_INDEX_FILE", "")
    if git_index_file:
        base = os.path.basename(git_index_file)
        if base.startswith("next-index-"):
            return True
    return False


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
    leases, status_err = _active_same_root_leases(repo_root)

    # Sweep A P2: fail-closed when capacity status is unavailable, unless
    # the operator explicitly opts in via GOALFLIGHT_COMMIT_GUARD_FAIL_OPEN=1.
    # Silent fail-open would let collisions through whenever the capacity
    # plumbing is broken — that's the same failure mode the guard exists
    # to prevent.
    if leases is None:
        if os.environ.get("GOALFLIGHT_COMMIT_GUARD_FAIL_OPEN") == "1":
            if args.json:
                print(json.dumps({"ok": True, "fail_open": True, "error": status_err}))
            return 0
        msg = (
            f"goal-flight commit guard: REFUSED ({status_err}).\n\n"
            "Cannot verify worker leases — failing closed to prevent "
            "potential WIP bundling. To proceed anyway:\n"
            "  - Fix the capacity status path (run "
            "    `GOALFLIGHT_PYTHON=<path-to-python> "
            "scripts/goalflight_capacity.py status --json` "
            "    to diagnose), OR\n"
            "  - Set `GOALFLIGHT_COMMIT_GUARD_FAIL_OPEN=1` to opt into "
            "    fail-open (use only if you know capacity is intentionally "
            "    not configured for this project).\n"
        )
        if args.json:
            print(json.dumps({"ok": False, "fail_closed": True, "error": status_err, "message": msg}))
        else:
            sys.stderr.write(msg)
        return 2

    if not leases:
        if args.json:
            print(json.dumps({"ok": True, "leases": []}))
        return 0

    # Sweep A P2: env override accepts only specific dispatch IDs.
    # `all` is reserved for explicit one-shot CLI flag use; setting
    # GOALFLIGHT_COMMIT_GUARD_OVERRIDE=all globally would disable the
    # guard for every future commit (too easy to set + forget).
    override_env = os.environ.get("GOALFLIGHT_COMMIT_GUARD_OVERRIDE", "")
    override_flag = args.override_active_leases
    env_ids = {x.strip() for x in override_env.split(",") if x.strip()}
    flag_ids = {x.strip() for x in override_flag.split(",") if x.strip()}
    if "all" in env_ids:
        msg = (
            "goal-flight commit guard: REFUSED.\n\n"
            "GOALFLIGHT_COMMIT_GUARD_OVERRIDE=all is not allowed via env.\n"
            "Use the CLI flag explicitly per-invocation:\n"
            "  --override-active-leases all\n"
            "Or list specific dispatch IDs in the env var.\n"
        )
        if args.json:
            print(json.dumps({"ok": False, "leases": leases, "message": msg}))
        else:
            sys.stderr.write(msg)
        return 2
    override_ids = env_ids | flag_ids
    if "all" in flag_ids:
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
