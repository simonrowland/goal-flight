#!/usr/bin/env python3
"""Tests for goalflight_status.py terse + this-repo-scoped surfaces."""
from __future__ import annotations

import io
import sys
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
    test_done_code()
    test_cli()
    if _FAILS:
        print(f"\n{len(_FAILS)} FAILED: {_FAILS}")
        return 1
    print("\nall goalflight_status tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
