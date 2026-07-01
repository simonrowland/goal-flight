#!/usr/bin/env python3
"""Tests for goalflight_status.py terse + this-repo-scoped surfaces."""
from __future__ import annotations

import io
import json
import os
import sys
import time
import tempfile
from contextlib import redirect_stderr, redirect_stdout
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


def sample_pressure_payload() -> dict:
    payload = sample_payload()
    payload["rate_pressure"] = {
        "schema": "goalflight.rate-pressure.v1",
        "threshold": 3,
        "window_seconds": 600,
        "providers_under_pressure": [
            {
                "scope": "agent",
                "provider": "openai",
                "budget_key": "agent:codex",
                "count": 3,
                "labels": ["codex"],
                "current_caps": {"codex": 10},
                "recommended_caps": {"codex": 5},
                "fallback_providers": ["cursor", "grok"],
            }
        ],
    }
    return payload


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
    check("terminal marker + worker dead -> 0", S.done_code({
        "classification": "complete",
        "terminal_state": "complete",
        "worker_pid": 333,
        "worker_still_alive": False,
    }) == 0)
    check("unknown_no_pid -> 2", S.done_code(recs["amb1"]) == 2)
    check("controller_dead no pid -> 0",
          S.done_code({"state": "controller_dead", "classification": "controller_dead"}) == 0)
    check("controller_dead live pid still terminal -> 0",
          S.done_code({
              "state": "controller_dead",
              "classification": "controller_dead",
              "worker_pid": 333,
              "worker_still_alive": True,
          }) == 0)
    detached_controller_dead = {
        "dispatch_id": "detached-live-controller-dead",
        "state": "orphaned",
        "reason": "controller_dead",
        "detached": True,
        "agent": "codex",
        "worker_pid": 444,
        "worker_identity": {"lstart": "Tue Jun  9 09:00:00 2026", "comm": "python3"},
        "project_root": "/repo/A",
        "started_at": S.goalflight_ledger.utc_now(),
    }
    orig_read_records = S.goalflight_ledger.read_records
    orig_identity_matches = S.goalflight_ledger.identity_matches
    try:
        S.goalflight_ledger.read_records = lambda: [detached_controller_dead]
        S.goalflight_ledger.identity_matches = lambda _record: (True, "live")
        rows = S.goalflight_ledger.status_payload()["records"]
        check("detached controller_dead live classified expected_live",
              rows[0].get("classification") == "expected_live")
        check("detached controller_dead live terminal unknown",
              rows[0].get("terminal_state") == "unknown")
        check("detached controller_dead live --done -> 1",
              S.done_code(rows[0]) == 1)

        S.goalflight_ledger.identity_matches = lambda _record: (False, "dead")
        dead_rows = S.goalflight_ledger.status_payload()["records"]
        check("detached controller_dead dead worker classified worker_dead",
              dead_rows[0].get("classification") == "worker_dead")
        check("detached controller_dead dead worker terminal worker_dead",
              dead_rows[0].get("terminal_state") == "worker_dead")
    finally:
        S.goalflight_ledger.read_records = orig_read_records
        S.goalflight_ledger.identity_matches = orig_identity_matches

    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "detached-success.tail"
        started = S.goalflight_ledger.utc_now()
        tail.write_text("work finished\nCOMPLETE: detached ok\n", encoding="utf-8")
        success_record = {
            **detached_controller_dead,
            "dispatch_id": "detached-success-controller-dead",
            "stdout_path": str(tail),
            "started_at": started,
        }
        orig_identity_matches = S.goalflight_ledger.identity_matches
        try:
            S.goalflight_ledger.identity_matches = lambda _record: (False, "dead")
            success_row = {
                **success_record,
                "classification": S.goalflight_ledger.classify(success_record),
                "terminal_state": "worker_dead",
            }
            reconciled = S._reconcile_output_tail_record(success_row)
            check("detached controller_dead dead worker + COMPLETE tail -> complete",
                  reconciled.get("classification") == "complete")
            check("detached controller_dead reconciled --done -> 0",
                  S.done_code(reconciled) == 0)
        finally:
            S.goalflight_ledger.identity_matches = orig_identity_matches
    timeout_summary = {
        "dispatch_id": "timeout-live",
        "classification": "idle_timeout",
        "agent": "codex",
        "worker_pid": 333,
        "worker_still_alive": True,
    }
    timeout_raw = {
        **timeout_summary,
        "worker_identity": {"lstart": "Tue Jun  9 09:00:00 2026", "comm": "python3"},
    }
    orig_read_records = S.goalflight_ledger.read_records
    orig_identity_matches = S.goalflight_ledger.identity_matches
    orig_pid_alive = S.goalflight_compat.pid_alive
    try:
        captured: list[dict] = []

        def identity_live(record: dict) -> tuple[bool, str]:
            captured.append(record)
            return True, "live"

        S.goalflight_ledger.read_records = lambda: [timeout_raw]
        S.goalflight_ledger.identity_matches = identity_live
        check("idle_timeout refreshes raw identity before live", S.done_code(timeout_summary) == 1)
        check("idle_timeout liveness used raw identity", captured and captured[-1] is timeout_raw)

        S.goalflight_ledger.identity_matches = lambda record: (False, "dead")
        check("idle_timeout stale cached worker -> 0", S.done_code(timeout_summary) == 0)

        S.goalflight_ledger.read_records = lambda: []
        S.goalflight_compat.pid_alive = lambda _pid: False
        check("idle_timeout cached flag without identity -> 0", S.done_code(timeout_summary) == 0)
    finally:
        S.goalflight_ledger.read_records = orig_read_records
        S.goalflight_ledger.identity_matches = orig_identity_matches
        S.goalflight_compat.pid_alive = orig_pid_alive
    watcher_summary = {
        "dispatch_id": "watcher-live",
        "state": "watcher_stopped",
        "classification": "watcher_stopped",
        "agent": "codex",
        "worker_pid": 444,
        "worker_still_alive": True,
    }
    watcher_raw = {
        **watcher_summary,
        "worker_identity": {"lstart": "Tue Jun  9 09:00:00 2026", "comm": "python3"},
    }
    orig_read_records = S.goalflight_ledger.read_records
    orig_identity_matches = S.goalflight_ledger.identity_matches
    orig_pid_alive = S.goalflight_compat.pid_alive
    try:
        captured = []

        def identity_live(record: dict) -> tuple[bool, str]:
            captured.append(record)
            return True, "live"

        S.goalflight_ledger.read_records = lambda: [watcher_raw]
        S.goalflight_ledger.identity_matches = identity_live
        check("watcher_stopped live marker -> 1", S.done_code(watcher_summary) == 1)
        check("watcher_stopped liveness used raw identity", captured and captured[-1] is watcher_raw)
        check(
            "ledger watcher_stopped live classified expected_live",
            S.goalflight_ledger.classify(watcher_raw) == "expected_live",
        )

        S.goalflight_ledger.identity_matches = lambda record: (False, "dead")
        check("watcher_stopped dead marker -> 0", S.done_code(watcher_summary) == 0)
        check(
            "watcher_stopped expected_live stale rechecks to 0",
            S.done_code({**watcher_summary, "classification": "expected_live"}) == 0,
        )
        check(
            "ledger watcher_stopped dead classified terminal",
            S.goalflight_ledger.classify(watcher_raw) == "watcher_stopped",
        )
    finally:
        S.goalflight_ledger.read_records = orig_read_records
        S.goalflight_ledger.identity_matches = orig_identity_matches
        S.goalflight_compat.pid_alive = orig_pid_alive
    check("stale_* -> 2", S.done_code({"classification": "stale_pid_reuse"}) == 2)
    check("missing classification -> 2 (do not claim done)", S.done_code({}) == 2)
    check(
        "running no terminal marker -> 2",
        S.done_code(
            {
                "dispatch_id": "between-turn",
                "state": "running",
                "classification": "running",
                "worker_pid": 333,
                "worker_still_alive": True,
            }
        )
        == 2,
    )


def test_output_tail_reconciles_success_marker_after_watcher_death() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tail = Path(tmp) / "dead-worker.tail"
        tail.write_text("work finished\nCOMPLETE: done from tail\n", encoding="utf-8")
        started = S.goalflight_ledger.utc_now()
        record = {
            "dispatch_id": "tail-reconciled",
            "classification": "stale_dead",
            "state": "running",
            "terminal_state": "unknown",
            "agent": "codex",
            "worker_pid": 999999,
            "worker_identity": {"lstart": "Tue Jun  9 09:00:00 2026", "comm": "python3"},
            "stdout_path": str(tail),
            "started_at": started,
        }
        orig_identity_matches = S.goalflight_ledger.identity_matches
        try:
            S.goalflight_ledger.identity_matches = lambda _record: (False, "dead")
            reconciled = S._reconcile_output_tail_record(record)
            check("stale dead + final COMPLETE tail -> complete",
                  reconciled.get("classification") == "complete")
            check("reconciled status is terminal success",
                  reconciled.get("terminal_state") == "complete" and S.done_code(reconciled) == 0)
            check("raw stale signal retained",
                  reconciled.get("raw_classification") == "stale_dead")
            check("dead-worker reconciliation records gate reason",
                  reconciled.get("output_tail_reconciliation", {}).get("promoted") is True)

            S.goalflight_ledger.identity_matches = lambda _record: (True, "live")
            fresh_live = S._reconcile_output_tail_record({**record, "classification": "watcher_stopped"})
            check("live worker with fresh terminal tail does not reconcile complete",
                  fresh_live.get("classification") == "watcher_stopped")
            check("fresh live unpromoted record carries NO terminal_marker signal",
                  fresh_live.get("terminal_marker") is None and fresh_live.get("terminal_marker_source") is None)
            check("fresh live marker kept as diagnostic observed_marker only",
                  fresh_live.get("output_tail_reconciliation", {}).get("observed_marker", {}).get("kind") == "COMPLETE")
            check("fresh live reconciliation is explicitly unpromoted",
                  fresh_live.get("output_tail_reconciliation", {}).get("promoted") is False)
            check("fresh live worker is NOT false-done (done_code != 0)",
                  S.done_code(fresh_live) != 0)

            os.utime(
                tail,
                (
                    time.time() - S._OUTPUT_TAIL_IDLE_RECONCILE_S - 5,
                    time.time() - S._OUTPUT_TAIL_IDLE_RECONCILE_S - 5,
                ),
            )
            idle_live = dict(record)
            idle_live.pop("started_at", None)
            idle_live["classification"] = "idle_timeout"
            idle_reconciled = S._reconcile_output_tail_record(idle_live)
            check("live worker with idle terminal tail stays live",
                  idle_reconciled.get("classification") == "idle_timeout")
            check("idle live reconciliation is explicitly unpromoted",
                  idle_reconciled.get("output_tail_reconciliation", {}).get("promoted") is False)
            check("idle live reconciliation records alive reason",
                  str(idle_reconciled.get("output_tail_reconciliation", {}).get("reason", "")).startswith("worker_alive_tail_idle:"))

            no_identity = dict(record)
            no_identity.pop("worker_identity", None)
            no_identity.pop("started_at", None)
            no_identity["classification"] = "watcher_stopped"
            no_identity_reconciled = S._reconcile_output_tail_record(no_identity)
            check("terminal tail without identity does not reconcile complete",
                  no_identity_reconciled.get("classification") == "watcher_stopped")
            check("identity-unavailable reconciliation reason surfaced",
                  no_identity_reconciled.get("output_tail_reconciliation", {}).get("reason") == "liveness_indeterminate")
        finally:
            S.goalflight_ledger.identity_matches = orig_identity_matches

        future = dict(record)
        future["started_at"] = "2999-01-01T00:00:00+00:00"
        unchanged = S._reconcile_output_tail_record(future)
        check("implausible old tail mtime does not reconcile",
              unchanged.get("classification") == "stale_dead")


def test_idle_timeout_live_hint_rendered() -> None:
    orig_identity_matches = S.goalflight_ledger.identity_matches
    payload = sample_payload()
    payload["dispatch"]["records"].append(
        {
            "dispatch_id": "timeout-live",
            "project_root": "/repo/A",
            "classification": "idle_timeout",
            "agent": "codex",
            "worker_pid": 333,
            "worker_still_alive": True,
            "worker_identity": {"lstart": "Tue Jun  9 09:00:00 2026", "comm": "python3"},
            "status_path": "/tmp/timeout-live.json",
        }
    )
    try:
        S.goalflight_ledger.identity_matches = lambda record: (True, "live")
        digest = "\n".join(S.render_text(S.scope_payload(payload, "/repo/A"), 10))
        check("idle-timeout live worker counted running", digest.splitlines()[0].startswith("A: running2"))
        check("idle-timeout live worker hint rendered",
              "worker still alive - re-attach via goalflight_status.py --wait timeout-live" in digest)
    finally:
        S.goalflight_ledger.identity_matches = orig_identity_matches


def test_rate_pressure_warning_rendered() -> None:
    digest = "\n".join(S.render_text(sample_pressure_payload(), 10))
    check("digest surfaces adaptive rate-pressure warning",
          "warning: adaptive rate pressure agent:codex" in digest)
    check("digest shows reduced codex cap", "codex 10->5" in digest)


def test_queue_pending_without_drainer_warns_in_json_and_text() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_env = os.environ.copy()
        orig_launchd = S._launchd_drainer_loaded
        orig_process = S._drain_process_running
        try:
            os.environ["GOALFLIGHT_STATE_DIR"] = str(Path(tmp) / "state")
            queue_dir = Path(tmp) / "state" / "dispatch-queue"
            queue_dir.mkdir(parents=True)
            (queue_dir / "pending.json").write_text("{}", encoding="utf-8")
            S._launchd_drainer_loaded = lambda: False
            S._drain_process_running = lambda: False

            payload = S.status_payload()
            warnings = payload.get("warnings", [])
            digest = "\n".join(S.render_text(payload, 10))
        finally:
            S._launchd_drainer_loaded = orig_launchd
            S._drain_process_running = orig_process
            os.environ.clear()
            os.environ.update(old_env)

        check("pending queue without drainer emits one warning", len(warnings) == 1)
        check("pending queue warning code stable",
              warnings and warnings[0].get("code") == "queue_pending_no_drainer")
        check("pending queue warning includes depth",
              warnings and warnings[0].get("queue_depth") == 1)
        check("pending queue warning text rendered",
              "WARN queue_pending_no_drainer" in digest)
        check("pending queue warning remedy rendered",
              "restore the scheduled drainer" in digest)


def test_queue_pending_warning_silent_when_empty_or_drainer_live() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        old_env = os.environ.copy()
        orig_launchd = S._launchd_drainer_loaded
        orig_process = S._drain_process_running
        try:
            os.environ["GOALFLIGHT_STATE_DIR"] = str(Path(tmp) / "state")
            queue_dir = Path(tmp) / "state" / "dispatch-queue"
            queue_dir.mkdir(parents=True)
            S._launchd_drainer_loaded = lambda: False
            S._drain_process_running = lambda: False
            check("empty queue emits no drainer warning", S._queue_drainer_warnings() == [])

            (queue_dir / "pending.json").write_text("{}", encoding="utf-8")
            S._launchd_drainer_loaded = lambda: True
            S._drain_process_running = lambda: False
            check("loaded drain agent suppresses queue warning", S._queue_drainer_warnings() == [])

            S._launchd_drainer_loaded = lambda: False
            S._drain_process_running = lambda: True
            check("running drain process suppresses queue warning", S._queue_drainer_warnings() == [])
        finally:
            S._launchd_drainer_loaded = orig_launchd
            S._drain_process_running = orig_process
            os.environ.clear()
            os.environ.update(old_env)


def test_drain_process_running_matches_only_real_invocation() -> None:
    """_drain_process_running must match a real `goalflight_dispatch.py drain`
    argv only -- never a lookalike path, the --no-drain-on-submit flag, or a
    prompt arg that merely contains the word (those would wrongly suppress the
    no-drainer WARN)."""
    orig_run = S.subprocess.run

    class _Proc:
        returncode = 0

        def __init__(self, out: str) -> None:
            self.stdout = out

    def make_ps(lines: list[str]):
        def fake_run(*_a, **_k):
            return _Proc("\n".join(lines) + "\n")
        return fake_run

    try:
        # Positive: a genuine drain invocation (abs path, `drain` subcommand).
        S.subprocess.run = make_ps([
            "/opt/homebrew/bin/python3 /Users/x/.goal-flight/skill/scripts/goalflight_dispatch.py drain --json",
        ])
        check("real drain invocation detected", S._drain_process_running() is True)

        # Negatives: lookalikes that must NOT count as a live drainer.
        S.subprocess.run = make_ps([
            "python3 scripts/goalflight_dispatch.py --submit --no-drain-on-submit --agent codex",
            "python3 scripts/not_goalflight_dispatch.py drain --json",
            "codex exec 'review goalflight_dispatch.py and drain the queue'",
            "python3 scripts/goalflight_dispatch.py --submit --drain-on-submit",
        ])
        check("no false-positive on submit/lookalike/prompt commands",
              S._drain_process_running() is False)
    finally:
        S.subprocess.run = orig_run


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


def test_wait_cli() -> None:
    orig_payload, orig_root = S.status_payload, S.this_project_root
    payload = sample_payload()
    payload["dispatch"]["records"].append(
        {
            "dispatch_id": "dead1",
            "project_root": "/repo/A",
            "classification": "idle_timeout",
            "agent": "codex",
            "worker_pid": 333,
            "worker_still_alive": True,
        }
    )
    S.status_payload = lambda: payload
    S.this_project_root = lambda: "/repo/A"
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = S.main(["--wait", "done1,dead1", "--wait-timeout", "0", "--poll-s", "0.01"])
        out = buf.getvalue()
        check("--wait all-terminal returns 0", rc == 0)
        check("--wait prints complete digest", out.splitlines()[0] == "wait complete: 2/2 terminal")
        check("--wait preserves idle_timeout label", "dead1 -> idle_timeout" in out)

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = S.main(
                [
                    "--wait",
                    "done1,dead1",
                    "--wait",
                    "amb1",
                    "--wait-timeout",
                    "0.01",
                    "--poll-s",
                    "0.01",
                ]
            )
        out = buf.getvalue()
        check("--wait timeout returns nonzero", rc == 1)
        check("--wait timeout reports pending id", "pending amb1" in out)
        check("--wait timeout keeps final per-id states", "amb1 -> timeout" in out)

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = S.main(["--wait", "done1", "--json", "--wait-timeout", "0"])
        payload_json = json.loads(buf.getvalue())
        check("--wait --json returns 0", rc == 0)
        check("--wait --json has dispatch row", payload_json["dispatches"][0]["dispatch_id"] == "done1")
    finally:
        S.status_payload, S.this_project_root = orig_payload, orig_root


def _wait_payload(dispatch_id: str, classification: str, *, terminal_state: str | None = None) -> dict:
    record = {
        "dispatch_id": dispatch_id,
        "project_root": "/repo/A",
        "classification": classification,
        "agent": "codex",
        "worker_pid": 333,
        "worker_still_alive": classification == "expected_live",
    }
    if terminal_state is not None:
        record["terminal_state"] = terminal_state
    return {
        "schema": "goalflight.status.aggregate.v1",
        "capacity": {"operating_cap": 16},
        "capacity_state": {"leases": {}, "cooldowns": {}},
        "dispatch": {"records": [record], "surplus_processes": []},
    }


def test_wait_default_timeout() -> None:
    orig_wait, orig_root = S.wait_for_dispatches, S.this_project_root
    seen: dict = {}

    def fake_wait(wait_ids, *, project_root, timeout_s, poll_s, json_output=False, **_kwargs):
        seen.update(
            {
                "wait_ids": wait_ids,
                "project_root": project_root,
                "timeout_s": timeout_s,
                "poll_s": poll_s,
                "json_output": json_output,
            }
        )
        return 7

    try:
        S.wait_for_dispatches = fake_wait
        S.this_project_root = lambda: "/repo/A"
        rc = S.main(["--wait", "live1"])
        check("--wait parser default is 1800s", rc == 7 and seen.get("timeout_s") == 1800.0)
        check("--wait parser keeps wait ids", seen.get("wait_ids") == ["live1"])
    finally:
        S.wait_for_dispatches, S.this_project_root = orig_wait, orig_root


def test_wait_unbounded_sentinels_and_positive_timeout() -> None:
    orig_payload = S.status_payload
    orig_sleep = S.time.sleep
    orig_monotonic = S.time.monotonic

    def run_unbounded(timeout_s: float | None) -> tuple[int, int, str]:
        calls = {"count": 0}

        def payload_sequence() -> dict:
            calls["count"] += 1
            if calls["count"] == 1:
                return _wait_payload("flip", "expected_live")
            return _wait_payload("flip", "complete", terminal_state="complete")

        S.status_payload = payload_sequence
        S.time.sleep = lambda _seconds: None
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = S.wait_for_dispatches(
                ["flip"],
                project_root="/repo/A",
                timeout_s=timeout_s,
                poll_s=0.01,
            )
        return rc, calls["count"], buf.getvalue()

    try:
        rc, calls, out = run_unbounded(0)
        check("--wait-timeout 0 waits unbounded until terminal", rc == 0 and calls == 2)
        check("--wait-timeout 0 reports eventual terminal state", "flip -> complete" in out)

        rc, calls, out = run_unbounded(None)
        check("internal None timeout waits unbounded until terminal", rc == 0 and calls == 2)
        check("internal None reports eventual terminal state", "flip -> complete" in out)

        S.status_payload = lambda: _wait_payload("pending1", "expected_live")
        times = iter([0.0, 0.2])
        S.time.monotonic = lambda: next(times)
        S.time.sleep = lambda _seconds: None
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = S.wait_for_dispatches(
                ["pending1"],
                project_root="/repo/A",
                timeout_s=0.1,
                poll_s=0.01,
            )
        out = buf.getvalue()
        check("positive --wait-timeout returns nonzero", rc == 1)
        check("positive --wait-timeout reports pending ids", "pending pending1" in out)
        check("positive --wait-timeout marks nonterminal timeout", "pending1 -> timeout" in out)
    finally:
        S.status_payload = orig_payload
        S.time.sleep = orig_sleep
        S.time.monotonic = orig_monotonic


def test_wait_keyboard_interrupt_returns_130_without_signal() -> None:
    orig_payload = S.status_payload
    orig_sleep = S.time.sleep
    orig_run = S.subprocess.run
    subprocess_calls: list[tuple] = []

    def fail_subprocess(*args, **kwargs):
        subprocess_calls.append(args)
        raise AssertionError("wait interrupt path must not shell out or signal workers")

    def payload_without_pid() -> dict:
        payload = _wait_payload("live1", "unknown_no_pid")
        payload["dispatch"]["records"][0].pop("worker_pid", None)
        return payload

    try:
        S.status_payload = payload_without_pid
        S.time.sleep = lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt())
        S.subprocess.run = fail_subprocess
        err = io.StringIO()
        with redirect_stderr(err):
            rc = S.wait_for_dispatches(
                ["live1"],
                project_root="/repo/A",
                timeout_s=1800.0,
                poll_s=0.01,
            )
        check("KeyboardInterrupt wait returns 130", rc == 130)
        check("KeyboardInterrupt wait prints re-attach hint",
              "goalflight_status.py --wait live1" in err.getvalue())
        check("KeyboardInterrupt wait sends no subprocess signal", subprocess_calls == [])
    finally:
        S.status_payload = orig_payload
        S.time.sleep = orig_sleep
        S.subprocess.run = orig_run


def test_wait_snapshot_uses_single_liveness_result() -> None:
    payload = sample_payload()
    payload["dispatch"]["records"].append(
        {
            "dispatch_id": "timeout-flip",
            "project_root": "/repo/A",
            "classification": "idle_timeout",
            "agent": "codex",
            "worker_pid": 333,
            "worker_still_alive": True,
            "worker_identity": {"lstart": "Tue Jun  9 09:00:00 2026", "comm": "python3"},
        }
    )
    orig_identity_matches = S.goalflight_ledger.identity_matches
    calls: list[dict] = []

    def identity_flips_if_called_twice(record: dict) -> tuple[bool, str]:
        calls.append(record)
        return (False, "dead") if len(calls) == 1 else (True, "live")

    try:
        S.goalflight_ledger.identity_matches = identity_flips_if_called_twice
        rows = S._wait_snapshot(payload, ["timeout-flip"])
        check("--wait snapshot evaluates timeout liveness once", len(calls) == 1)
        check("--wait snapshot keeps terminal decision stable", rows[0]["terminal"] is True)
        check("--wait snapshot preserves idle-timeout label", rows[0]["state"] == "idle_timeout")
    finally:
        S.goalflight_ledger.identity_matches = orig_identity_matches


def main() -> int:
    test_scope()
    test_worktree_scope()
    test_worktree_scope_symlinked_root()
    test_done_code()
    test_output_tail_reconciles_success_marker_after_watcher_death()
    test_idle_timeout_live_hint_rendered()
    test_rate_pressure_warning_rendered()
    test_queue_pending_without_drainer_warns_in_json_and_text()
    test_queue_pending_warning_silent_when_empty_or_drainer_live()
    test_drain_process_running_matches_only_real_invocation()
    test_cli()
    test_wait_cli()
    test_wait_default_timeout()
    test_wait_unbounded_sentinels_and_positive_timeout()
    test_wait_keyboard_interrupt_returns_130_without_signal()
    test_wait_snapshot_uses_single_liveness_result()
    if _FAILS:
        print(f"\n{len(_FAILS)} FAILED: {_FAILS}")
        return 1
    print("\nall goalflight_status tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
