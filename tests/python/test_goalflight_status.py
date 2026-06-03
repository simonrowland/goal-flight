#!/usr/bin/env python3
"""Tests for goalflight_status.py terse + this-repo-scoped surfaces."""
from __future__ import annotations

import io
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_status as S

_FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _FAILS.append(name)


def sample_payload() -> dict:
    return {
        "schema": "goalflight.status.aggregate.v1",
        "capacity": {"operating_cap": 16},
        "capacity_state": {
            "leases": {
                "l1": {"state": "active", "project_root": "/repo/A"},
                "l2": {"state": "active", "project_root": "/repo/B"},
                "l3": {"state": "expired", "project_root": "/repo/A"},
            },
            "cooldowns": {},
        },
        "dispatch": {
            "records": [
                {"dispatch_id": "live1", "project_root": "/repo/A",
                 "classification": "expected_live", "agent": "codex",
                 "worker_pid": 111, "worker_still_alive": True,
                 "status_path": "/tmp/live1.json"},
                {"dispatch_id": "done1", "project_root": "/repo/A",
                 "classification": "complete", "agent": "grok",
                 "terminal_state": "complete"},
                {"dispatch_id": "amb1", "project_root": "/repo/A",
                 "classification": "unknown_no_pid", "agent": "codex"},
                {"dispatch_id": "other1", "project_root": "/repo/B",
                 "classification": "complete", "agent": "codex"},
            ],
            "surplus_processes": [],
        },
    }


def test_scope() -> None:
    p = S.scope_payload(sample_payload(), "/repo/A")
    ids = {r["dispatch_id"] for r in p["dispatch"]["records"]}
    check("scope keeps only this-project records", ids == {"live1", "done1", "amb1"})
    check("scope keeps only this-project leases", set(p["capacity_state"]["leases"]) == {"l1", "l3"})
    check("machine_active counts ALL projects", p["scope"]["machine_active_leases"] == 2)
    allp = S.scope_payload(sample_payload(), None)
    check("all-projects keeps every record", len(allp["dispatch"]["records"]) == 4)
    check("all-projects still reports machine count", allp["scope"]["machine_active_leases"] == 2)


def worktree_payload(repo_root: str, worktree_dir: str) -> dict:
    """A worktree-style ACP dispatch: the worker runs inside a per-dispatch
    worktree (``worker_cwd``/``worktree_path`` = worktree dir) but the ledger
    record's ``project_root`` MUST be the main repo toplevel so the record stays
    in-scope. This is the exact data shape of the e21cda2 regression: if
    goalflight_acp_run recorded ``project_root=worker_cwd`` (the worktree dir)
    the record would be scoped OUT of ``status`` for its whole lifetime."""
    return {
        "schema": "goalflight.status.aggregate.v1",
        "capacity": {"operating_cap": 16},
        "capacity_state": {
            "leases": {
                # Lease already records the main-repo project_root (acp_run
                # line ~1084 uses str(project_root), not worker_cwd).
                "wl1": {"state": "active", "project_root": repo_root},
            },
            "cooldowns": {},
        },
        "dispatch": {
            "records": [
                {"dispatch_id": "wt-live", "project_root": repo_root,
                 "worktree_path": worktree_dir, "worker_cwd": worktree_dir,
                 "classification": "expected_live", "agent": "codex",
                 "worker_pid": 222, "worker_still_alive": True,
                 "status_path": f"{worktree_dir}/.goalflight-wt-live.status.json"},
            ],
            "surplus_processes": [],
        },
    }


def test_worktree_scope() -> None:
    repo_root = "/repo/A"
    worktree_dir = "/repo/A/worktrees/wt-live"
    # In-scope check: a worktree worker whose project_root is the main repo
    # toplevel survives scope_payload and is classified LIVE (exit 1), not
    # scoped-out-and-ambiguous (exit 2). This is the regression assertion.
    p = S.scope_payload(worktree_payload(repo_root, worktree_dir), repo_root)
    ids = {r["dispatch_id"] for r in p["dispatch"]["records"]}
    check("worktree record stays in-scope under main repo root", ids == {"wt-live"})
    check("worktree lease stays in-scope", set(p["capacity_state"]["leases"]) == {"wl1"})
    rec = S.find_record(p, "wt-live")
    check("worktree record is findable in-scope", rec is not None)
    check("worktree --done classifies LIVE (1), not ambiguous (2)",
          S.done_code(rec) == 1)

    # Regression guard: if acp_run had recorded the WORKTREE dir as project_root
    # (the e21cda2 bug), scoping to the main repo root would DROP the record and
    # --done would fall through to ambiguous (exit 2). Encode that the buggy
    # shape is exactly the failure mode the fix prevents.
    buggy = worktree_payload(repo_root, worktree_dir)
    for r in buggy["dispatch"]["records"]:
        r["project_root"] = worktree_dir  # the regression: project_root=worker_cwd
    bp = S.scope_payload(buggy, repo_root)
    check("buggy worktree-as-project_root WOULD be scoped out",
          S.find_record(bp, "wt-live") is None)

    # --done exit-code path end-to-end (mirrors test_cli wiring): a correctly
    # recorded worktree worker returns 1 (live), a buggy one returns 2 (ambig).
    orig_payload, orig_root = S.status_payload, S.this_project_root
    S.this_project_root = lambda: repo_root
    try:
        S.status_payload = lambda: worktree_payload(repo_root, worktree_dir)
        check("--done wt-live -> 1 (live, in-scope)", S.main(["--done", "wt-live"]) == 1)
        S.status_payload = lambda: buggy
        check("--done wt-live (buggy shape) -> 2 (scoped out, ambiguous)",
              S.main(["--done", "wt-live"]) == 2)
    finally:
        S.status_payload, S.this_project_root = orig_payload, orig_root


def test_worktree_scope_symlinked_root() -> None:
    """Symlink edge: when the repo path is reached via a symlink, both sides
    resolve consistently. this_project_root() and --project both call
    Path(...).resolve(); acp_run records str(Path(cfg.cwd).resolve()). So a
    record whose project_root is the RESOLVED real path stays in-scope even when
    the caller scopes via --project <symlink-path>, because --project resolves
    too. Assert the resolved real path is what scoping matches on."""
    with tempfile.TemporaryDirectory() as tmp:
        real = Path(tmp) / "real-repo"
        real.mkdir()
        link = Path(tmp) / "link-repo"
        try:
            link.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            check("symlink edge skipped (unsupported platform)", True)
            return
        real_root = str(real.resolve())
        worktree_dir = str(real / "worktrees" / "wt-sym")
        payload = worktree_payload(real_root, worktree_dir)

        # Scope using the resolved real path: in-scope.
        p = S.scope_payload(payload, real_root)
        check("symlinked-repo worktree record in-scope via resolved root",
              S.find_record(p, "wt-live") is not None)

        # Caller passes the SYMLINK path via --project; main() resolves it to the
        # same real path, so the record still matches.
        orig_payload = S.status_payload
        S.status_payload = lambda: worktree_payload(real_root, worktree_dir)
        try:
            check("--project <symlink> --done wt-live -> 1 (resolves to real root)",
                  S.main(["--project", str(link), "--done", "wt-live"]) == 1)
        finally:
            S.status_payload = orig_payload


def test_done_code() -> None:
    recs = {r["dispatch_id"]: r for r in sample_payload()["dispatch"]["records"]}
    check("live -> 1", S.done_code(recs["live1"]) == 1)
    check("terminal complete -> 0", S.done_code(recs["done1"]) == 0)
    check("unknown_no_pid -> 2", S.done_code(recs["amb1"]) == 2)
    check("stale_* -> 2", S.done_code({"classification": "stale_pid_reuse"}) == 2)
    check("missing classification -> 2 (do not claim done)", S.done_code({}) == 2)


def test_cli() -> None:
    orig_payload, orig_root = S.status_payload, S.this_project_root
    S.status_payload = sample_payload
    S.this_project_root = lambda: "/repo/A"
    try:
        check("--done live1 -> 1 (live)", S.main(["--done", "live1"]) == 1)
        check("--done done1 -> 0 (terminal)", S.main(["--done", "done1"]) == 0)
        check("--done amb1 -> 2 (ambiguous)", S.main(["--done", "amb1"]) == 2)
        check("--done other1 scoped-out -> 2", S.main(["--done", "other1"]) == 2)
        check("--all-projects --done other1 -> 0", S.main(["--all-projects", "--done", "other1"]) == 0)
        check("--done missing -> 2", S.main(["--done", "nope"]) == 2)

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = S.main(["--dispatch", "live1"])
        out = buf.getvalue()
        check("--dispatch returns 0", rc == 0)
        check("--dispatch is one line", out.count("\n") == 1)
        check("--dispatch shows id + drill-down path", "live1" in out and "/tmp/live1.json" in out)

        buf = io.StringIO()
        with redirect_stdout(buf):
            S.main(["--limit", "10"])
        digest = buf.getvalue()
        check("digest summary uses scoped label", digest.splitlines()[0].startswith("A:"))
        check("digest excludes other-project record", "other1" not in digest)
        check("digest stays small (<1KB)", len(digest) < 1024)
    finally:
        S.status_payload, S.this_project_root = orig_payload, orig_root


def main() -> int:
    test_scope()
    test_worktree_scope()
    test_worktree_scope_symlinked_root()
    test_done_code()
    test_cli()
    if _FAILS:
        print(f"\n{len(_FAILS)} FAILED: {_FAILS}")
        return 1
    print("\nall goalflight_status tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
