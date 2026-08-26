#!/usr/bin/env python3
"""Watch-layer three-legged stall-candidate detector.

Field cases this pins:

* True positive: tail stale AND tree quiet AND cumulative CPU flat across two
  samples, all sustained for the threshold → ``worker_stalled_candidate``
  with evidence. This is a candidate/attention flag, not a verdict.
* False positive caught in the field: tail stale, CPU 0.0, but the worktree
  was written 90 seconds earlier → not flagged. The tail lags while a grok
  worker edits files.
* Remote-wait false positive: all three legs match a healthy worker blocked
  on a remote/studio job. Mitigated by candidate semantics, not another leg.
* Emit once on enter and once on recover, not per poll.
* Detect, do not kill: the process is still alive and may hold finished work.
* Must not trip worker_dead / terminal-authority handling.

Tests inject mtimes and CPU readings. They do not sleep real minutes.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Callable
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_chunk_summary as chunk_summary  # noqa: E402
import goalflight_compat  # noqa: E402
import goalflight_dispatch_states as states  # noqa: E402
import goalflight_watch as watch  # noqa: E402


THRESHOLD_S = 300.0
POLL_SECS = 0.125
DISPATCH_ID = "b229-wedge-detect"


def _isolate_env(tmp: Path) -> dict[str, str]:
    return {
        "GOALFLIGHT_STATE_DIR": str(tmp / "state"),
        "GOALFLIGHT_DISPATCH_DIR": str(tmp / "state" / "dispatch"),
        "GOALFLIGHT_TASK_STORE_DIR": str(tmp / "task-store"),
        "GOALFLIGHT_JOURNAL_DIR": str(tmp / "journal"),
        "GOALFLIGHT_MESSAGES_DIR": str(tmp / "messages"),
        "GOALFLIGHT_WAKE_LEDGER": str(tmp / "wake-ledger.jsonl"),
        "GOALFLIGHT_WAKE_LEDGER_DIR": str(tmp / "wake-ledger"),
        "GOAL_FLIGHT_PIDFILE_DIR": str(tmp / "pids"),
        "GOALFLIGHT_CAPACITY_CONF": "/dev/null",
        "GOALFLIGHT_TEST_MODE": "1",
    }


def _wedge(**overrides):
    kwargs = {
        "worker_alive": True,
        "tail_age_s": 400.0,
        "tree_age_s": 400.0,
        "cpu_delta_s": 0.0,
        "sample_interval_s": 5.0,
        "threshold_s": THRESHOLD_S,
    }
    kwargs.update(overrides)
    return watch.classify_worker_wedge(**kwargs)


def test_true_positive_all_three_legs_is_stall_candidate() -> None:
    evidence = _wedge()
    assert evidence is not None, "all three legs sustained must flag a stall candidate"
    assert evidence["state"] == watch.WORKER_STALLED_CANDIDATE_STATE
    assert evidence["authoritative"] is False
    assert "remote" in str(evidence["caveat"]).lower()
    assert evidence["tail_age_s"] == 400.0
    assert evidence["tree_age_s"] == 400.0
    assert evidence["cpu_delta_s"] == 0.0
    assert evidence["sample_interval_s"] == 5.0
    assert evidence["threshold_s"] == THRESHOLD_S
    assert evidence["tail_bytes_grown"] == 0


def test_default_sustain_is_fifteen_minutes_not_five() -> None:
    """5 minutes sits inside grok burst-gap range; 15 minutes is the default."""
    assert watch.DEFAULT_WEDGE_IDLE_SECS == 900.0
    # Injected 5-minute window still classifies when tests ask for it.
    assert _wedge(threshold_s=300.0) is not None
    # Default window: 400s of silence is not enough.
    assert _wedge(threshold_s=watch.DEFAULT_WEDGE_IDLE_SECS) is None


def test_remote_wait_matches_all_three_legs_but_is_only_a_candidate() -> None:
    """A healthy worker blocked on a remote/studio job is CPU-0, tail-quiet,
    and tree-quiet indefinitely. The three legs cannot distinguish it from a
    wedge. Classification is therefore a candidate, never a verdict.
    """
    evidence = _wedge(
        tail_age_s=2000.0,
        tree_age_s=2000.0,
        cpu_delta_s=0.0,
        sample_interval_s=30.0,
        threshold_s=900.0,
        tail_bytes_grown=0,
    )
    assert evidence is not None
    assert evidence["state"] == "worker_stalled_candidate"
    assert evidence["authoritative"] is False
    assert "remote-wait" in evidence["caveat"]
    assert evidence["tail_bytes_grown"] == 0
    assert states.is_terminal_state(evidence["state"]) is False


def test_tail_bytes_grown_is_recorded_not_used_as_a_leg() -> None:
    evidence = _wedge(tail_bytes_grown=42)
    assert evidence is not None
    assert evidence["tail_bytes_grown"] == 42
    assert evidence["state"] == "worker_stalled_candidate"


def test_false_positive_recent_tree_write_is_not_wedged() -> None:
    """Measured field miss: tail stale, CPU 0, tree written 90s ago."""
    evidence = _wedge(tail_age_s=400.0, tree_age_s=90.0, cpu_delta_s=0.0)
    assert evidence is None, (
        "tree written 90s ago must veto wedge even when tail is stale and CPU is flat"
    )


def test_fresh_tail_is_not_wedged() -> None:
    assert _wedge(tail_age_s=30.0) is None


def test_cpu_moving_is_not_wedged() -> None:
    assert _wedge(cpu_delta_s=1.25) is None


def test_missing_cpu_delta_is_not_wedged() -> None:
    """A single snapshot cannot prove CPU is flat; need a pair."""
    assert _wedge(cpu_delta_s=None, sample_interval_s=None) is None
    assert _wedge(cpu_delta_s=0.0, sample_interval_s=None) is None
    assert _wedge(cpu_delta_s=None, sample_interval_s=5.0) is None


def test_dead_process_is_not_worker_wedged() -> None:
    """Gone is worker_dead territory. Wedged means alive but stuck."""
    assert _wedge(worker_alive=False) is None


def test_tree_mtime_walker_sees_recent_write(tmp_path: Path | None = None) -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        old = root / "src" / "old.py"
        old.parent.mkdir()
        old.write_text("x\n", encoding="utf-8")
        recent = root / "src" / "recent.py"
        recent.write_text("y\n", encoding="utf-8")
        now = 1_700_000_000.0
        os.utime(old, (now - 400.0, now - 400.0))
        os.utime(recent, (now - 90.0, now - 90.0))
        newest = watch.newest_mtime_under(root)
        assert newest is not None
        tree_age = now - newest
        assert 80.0 <= tree_age <= 100.0, tree_age
        assert _wedge(tree_age_s=tree_age, tail_age_s=240.0) is None


def test_tree_mtime_walker_quiet_tree_is_old(tmp_path: Path | None = None) -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "src" / "lib.py"
        src.parent.mkdir()
        src.write_text("x\n", encoding="utf-8")
        now = 1_700_000_000.0
        os.utime(src, (now - 400.0, now - 400.0))
        newest = watch.newest_mtime_under(root)
        assert newest is not None
        tree_age = now - newest
        assert tree_age >= THRESHOLD_S
        evidence = _wedge(tree_age_s=tree_age)
        assert evidence is not None
        assert evidence["tree_age_s"] == tree_age


def test_git_dir_write_does_not_count_as_tree_activity() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "file.py"
        src.write_text("x\n", encoding="utf-8")
        git_index = root / ".git" / "index"
        git_index.parent.mkdir()
        git_index.write_text("idx\n", encoding="utf-8")
        now = 1_700_000_000.0
        os.utime(src, (now - 400.0, now - 400.0))
        os.utime(git_index, (now - 5.0, now - 5.0))
        newest = watch.newest_mtime_under(root)
        assert newest is not None
        tree_age = now - newest
        assert tree_age >= THRESHOLD_S, tree_age


def test_emit_once_on_enter_and_once_on_recover() -> None:
    events: list[str] = []
    was = False
    for is_wedged in (False, True, True, True, False, False, True):
        kind = watch.wedge_transition(was_wedged=was, is_wedged=is_wedged)
        if kind is not None:
            events.append(kind)
        was = is_wedged
    assert events == ["enter", "recover", "enter"], events


def test_emit_wedge_event_prints_once_shaped_record() -> None:
    evidence = _wedge()
    assert evidence is not None
    buf = io.StringIO()
    with patch.object(sys, "stdout", buf):
        watch.emit_wedge_event("enter", DISPATCH_ID, evidence)
        watch.emit_wedge_event("recover", DISPATCH_ID, evidence)
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    assert len(lines) == 2, lines
    assert lines[0].startswith("WATCHER-STALL-CANDIDATE ")
    assert lines[1].startswith("WATCHER-STALL-CLEAR ")
    entered = json.loads(lines[0].split(" ", 1)[1])
    recovered = json.loads(lines[1].split(" ", 1)[1])
    assert entered["dispatch_id"] == DISPATCH_ID
    assert entered["state"] == watch.WORKER_STALLED_CANDIDATE_STATE
    assert entered["authoritative"] is False
    for key in ("tail_age_s", "tree_age_s", "cpu_delta_s", "sample_interval_s", "tail_bytes_grown"):
        assert key in entered, entered
        assert key in recovered, recovered


def test_stall_candidate_is_not_terminal_and_not_worker_dead() -> None:
    assert watch.WORKER_STALLED_CANDIDATE_STATE == "worker_stalled_candidate"
    assert states.is_terminal_state("worker_stalled_candidate") is False
    assert states.is_running_state("worker_stalled_candidate") is False
    assert states.terminal_state_for("worker_stalled_candidate") == "unknown"
    assert states.is_terminal_state("worker_dead") is True
    assert states.terminal_state_for("worker_dead") == "worker_dead"
    assert states.is_terminal_state("wedged") is True
    assert states.terminal_state_for("wedged") == "error"


def test_finish_ledger_refuses_to_terminalize_stall_candidate() -> None:
    """Salvage candidate must not commit terminal authority (process still lives)."""
    result = watch._finish_existing_ledger(
        "alive-but-stuck",
        "worker_stalled_candidate",
        "stall_candidate",
        worker_still_alive=True,
    )
    assert result is None


def test_apply_wedge_does_not_kill_or_terminal_write() -> None:
    kills: list[tuple] = []

    def forbidden_kill(*args, **kwargs):
        kills.append((args, kwargs))
        raise AssertionError("wedge detector must not kill the worker")

    payload = {
        "state": "running",
        "worker_alive": True,
        "liveness_state": "running",
    }
    evidence = _wedge()
    assert evidence is not None
    with patch("os.kill", side_effect=forbidden_kill), patch(
        "signal.signal", lambda *_args: None
    ):
        applied = watch.apply_worker_wedge(
            payload,
            evidence=evidence,
            previously_wedged=False,
            dispatch_id=DISPATCH_ID,
        )
    assert kills == [], kills
    assert applied["event"] == "enter"
    assert payload["state"] == "worker_stalled_candidate"
    assert payload["liveness_state"] == "worker_stalled_candidate"
    assert payload["wedge_evidence"]["tail_age_s"] == 400.0
    assert payload["wedge_evidence"]["authoritative"] is False
    assert payload.get("terminal_write") is not True


def _run_wedge_watcher(
    monkeypatch_argv: list[str],
    tmp: Path,
    *,
    tree: Path,
    cpu_samples: list[dict[int, float]],
    on_poll_sleep: Callable[[int, Path], None],
    monotonic_start: float = 10_000.0,
) -> tuple[int, list[dict], str]:
    tail = tmp / "worker.tail"
    status = tmp / "watcher.status.json"
    env = _isolate_env(tmp)
    payloads: list[dict] = []
    poll_sleeps = 0
    real_write_status = watch.write_status
    cpu_iter = iter(cpu_samples)
    last_cpu = cpu_samples[-1] if cpu_samples else {424242: 1.0}
    monotonic = {"now": monotonic_start}

    def capture_status(path: Path, payload: dict) -> None:
        payloads.append(json.loads(json.dumps(payload)))
        real_write_status(path, payload)

    def controlled_sleep(seconds: float) -> None:
        nonlocal poll_sleeps
        if seconds == POLL_SECS:
            on_poll_sleep(poll_sleeps, tail)
            poll_sleeps += 1
            monotonic["now"] += seconds
            return
        monotonic["now"] += seconds

    def next_cpu(_pgid=None):
        try:
            return dict(next(cpu_iter))
        except StopIteration:
            return dict(last_cpu)

    stdout = io.StringIO()
    with patch.dict(os.environ, env, clear=False), patch.object(
        sys, "argv", monkeypatch_argv
    ), patch.object(watch, "write_status", capture_status), patch.object(
        watch.time, "sleep", controlled_sleep
    ), patch.object(
        watch.atexit, "register", lambda _callback: None
    ), patch.object(
        watch.signal, "signal", lambda *_args: None
    ), patch.object(
        watch, "worker_alive", lambda pid, _identity: (True, "live", {"pid": pid})
    ), patch.object(
        watch, "process_group_id", lambda pid: pid
    ), patch.object(
        watch, "pgroup_cpu_pct", lambda _pgid: 0.0
    ), patch.object(
        watch, "pgroup_cputime_snapshot", next_cpu
    ), patch.object(
        watch, "system_starved", lambda: False
    ), patch.object(
        watch.TraceLiveness, "sample", lambda self, **_kwargs: {}
    ), patch.object(
        watch, "active_monotonic", lambda: monotonic["now"]
    ), patch.object(
        sys, "stdout", stdout
    ), patch(
        "os.kill", side_effect=AssertionError("watcher must not kill a wedged worker")
    ):
        rc = watch.main()
    return rc, payloads, stdout.getvalue()


def test_watcher_loop_true_positive_emits_once_and_stays_alive() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        tree = tmp / "worktree"
        src = tree / "app.py"
        src.parent.mkdir()
        src.write_text("print(1)\n", encoding="utf-8")
        tail = tmp / "worker.tail"
        tail.write_text("STATUS: investigating mid-senten", encoding="utf-8")
        now = time.time()
        os.utime(src, (now - 400.0, now - 400.0))
        os.utime(tail, (now - 400.0, now - 400.0))
        status = tmp / "watcher.status.json"
        argv = [
            "goalflight_watch.py",
            "--pid",
            "424242",
            "--tail",
            str(tail),
            "--status-json",
            str(status),
            "--dispatch-id",
            DISPATCH_ID,
            "--poll-secs",
            str(POLL_SECS),
            "--max-idle-secs",
            "999999",
            "--wedge-idle-secs",
            "300",
            "--project-root",
            str(tmp / "canonical"),
            "--worker-cwd",
            str(tree),
            "--agent",
            "grok",
        ]
        (tmp / "canonical").mkdir()

        def on_poll(index: int, tail_path: Path) -> None:
            if index == 0:
                # First real poll after the initial sample pair: should already
                # be wedged. Keep watching.
                return
            if index == 3:
                tail_path.write_text(
                    "STATUS: investigating mid-sentence\n"
                    f"COMPLETE: {DISPATCH_ID} — recovered\n",
                    encoding="utf-8",
                )
                return
            if index > 6:
                raise AssertionError("watcher did not observe the terminal marker")

        rc, payloads, output = _run_wedge_watcher(
            argv,
            tmp,
            tree=tree,
            cpu_samples=[{424242: 12.0}, {424242: 12.0}, {424242: 12.0}, {424242: 12.0}],
            on_poll_sleep=on_poll,
        )
        wedged = [p for p in payloads if p.get("state") == "worker_stalled_candidate"]
        assert wedged, [p.get("state") for p in payloads]
        evidence = wedged[0].get("wedge_evidence") or {}
        for key in ("tail_age_s", "tree_age_s", "cpu_delta_s", "sample_interval_s", "tail_bytes_grown"):
            assert key in evidence, evidence
        assert evidence["cpu_delta_s"] == 0.0
        assert evidence["authoritative"] is False
        assert output.count("WATCHER-STALL-CANDIDATE ") == 1, output
        assert rc == 0
        assert payloads[-1].get("state") == "complete"


def test_watcher_loop_recent_tree_write_never_wedges() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        tree = tmp / "worktree"
        src = tree / "app.py"
        src.parent.mkdir()
        src.write_text("print(1)\n", encoding="utf-8")
        tail = tmp / "worker.tail"
        tail.write_text("STATUS: still editing files\n", encoding="utf-8")
        now = time.time()
        os.utime(src, (now - 90.0, now - 90.0))
        os.utime(tail, (now - 400.0, now - 400.0))
        status = tmp / "watcher.status.json"
        argv = [
            "goalflight_watch.py",
            "--pid",
            "424242",
            "--tail",
            str(tail),
            "--status-json",
            str(status),
            "--dispatch-id",
            DISPATCH_ID,
            "--poll-secs",
            str(POLL_SECS),
            "--max-idle-secs",
            "999999",
            "--wedge-idle-secs",
            "300",
            "--project-root",
            str(tmp / "canonical"),
            "--worker-cwd",
            str(tree),
            "--agent",
            "grok",
        ]
        (tmp / "canonical").mkdir()

        def on_poll(index: int, tail_path: Path) -> None:
            if index == 2:
                tail_path.write_text(
                    "STATUS: still editing files\n"
                    f"COMPLETE: {DISPATCH_ID} — edited\n",
                    encoding="utf-8",
                )
                return
            if index > 6:
                raise AssertionError("watcher did not observe the terminal marker")

        rc, payloads, output = _run_wedge_watcher(
            argv,
            tmp,
            tree=tree,
            cpu_samples=[{424242: 4.0}, {424242: 4.0}, {424242: 4.0}],
            on_poll_sleep=on_poll,
        )
        assert all(p.get("state") != "worker_stalled_candidate" for p in payloads), [
            p.get("state") for p in payloads
        ]
        assert "WATCHER-STALL-CANDIDATE " not in output, output
        assert rc == 0


def test_status_overlay_surfaces_worker_wedged_without_terminalizing() -> None:
    import goalflight_status as status

    with tempfile.TemporaryDirectory() as td:
        sidecar = Path(td) / "d.status.json"
        sidecar.write_text(
            json.dumps(
                {
                    "dispatch_id": "d",
                    "state": "worker_stalled_candidate",
                    "reason": "worker_stalled_candidate",
                    "wedge_evidence": {
                        "tail_age_s": 400.0,
                        "tree_age_s": 400.0,
                        "cpu_delta_s": 0.0,
                        "sample_interval_s": 5.0,
                        "threshold_s": 300.0,
                    },
                }
            ),
            encoding="utf-8",
        )
        record = {
            "dispatch_id": "d",
            "state": "running",
            "classification": "expected_live",
            "status_path": str(sidecar),
        }
        out = status._decorate_trace_status(record)
        assert out["state"] == "worker_stalled_candidate"
        assert out["liveness_state"] == "worker_stalled_candidate"
        assert out["wedge_evidence"]["cpu_delta_s"] == 0.0
        assert states.is_terminal_state(out["state"]) is False
        assert status.done_code(out, worker_alive=True) == 1
        assert status._dashboard_count_bucket(out) == "stalled"


def test_cputime_delta_pairs_pids_instead_of_group_sum() -> None:
    from goalflight_liveness import cputime_delta_seconds

    before = {1: 10.0, 2: 3.0}
    after = {1: 10.0}  # child 2 exited
    assert cputime_delta_seconds(before, after) == 0.0
    after_born = {1: 10.0, 3: 0.4}
    assert abs(cputime_delta_seconds(before, after_born) - 0.4) < 1e-9


def _write_dispatch_record(
    tmp: Path,
    *,
    dispatch_id: str,
    project_root: Path,
    worker_cwd: Path | None,
) -> Path:
    env = _isolate_env(tmp)
    runs = Path(env["GOALFLIGHT_STATE_DIR"]) / "runs.d"
    runs.mkdir(parents=True, exist_ok=True)
    path = runs / f"{goalflight_compat.safe_dispatch_filename(dispatch_id)}.json"
    record = {
        "schema": "goalflight.dispatch.v1",
        "dispatch_id": dispatch_id,
        "project_root": str(project_root),
        "state": "running",
    }
    if worker_cwd is not None:
        record["worker_cwd"] = str(worker_cwd)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def _dual_tree_layout(tmp: Path) -> tuple[Path, Path, Path]:
    """Canonical root with a sibling's recent write + a quiet per-dispatch cwd."""
    canonical = tmp / "repo"
    other = canonical / "worktrees" / "other-dispatch" / "busy.py"
    other.parent.mkdir(parents=True)
    other.write_text("other worker still editing\n", encoding="utf-8")
    worker = canonical / "worktrees" / "this-dispatch"
    src = worker / "app.py"
    src.parent.mkdir(parents=True)
    src.write_text("print(1)\n", encoding="utf-8")
    return canonical, worker, src


def test_tree_leg_scans_distinct_worker_cwd_not_canonical_root() -> None:
    """P1: a real per-dispatch cwd is scanned; the shared root is not."""
    canonical = Path("/repo")
    worker = Path("/repo/worktrees/b229")
    leg = watch.resolve_wedge_tree_leg(
        {"project_root": str(canonical), "worker_cwd": str(worker)},
        project_root=canonical,
    )
    assert leg["kind"] == "worker_cwd", leg
    assert Path(leg["scan_root"]) == worker.resolve() or Path(str(leg["scan_root"])) == worker
    assert "repo/worktrees/b229" in str(leg["scan_root"]).replace("\\", "/")


def test_tree_leg_indeterminate_when_cwd_is_canonical_root() -> None:
    """P1: root-rooted (or pre-b217) records cannot discriminate sibling writes."""
    root = Path("/repo")
    leg = watch.resolve_wedge_tree_leg(
        {"project_root": str(root), "worker_cwd": str(root)},
        project_root=root,
    )
    assert leg["kind"] == "indeterminate", leg
    assert leg["scan_root"] is None
    assert "canonical" in str(leg.get("reason") or "").lower() or "root" in str(
        leg.get("reason") or ""
    ).lower()


def test_tree_leg_indeterminate_when_worker_cwd_missing() -> None:
    root = Path("/repo")
    leg = watch.resolve_wedge_tree_leg(
        {"project_root": str(root)},
        project_root=root,
    )
    assert leg["kind"] == "indeterminate", leg
    assert leg["scan_root"] is None


def test_chunk_summary_treats_stall_candidate_as_live_not_missing() -> None:
    """P3: novel candidate must not vanish as 'missing'; still not a verdict."""
    assert (
        chunk_summary.normalize_state(
            {"state": "running"},
            {"state": "worker_stalled_candidate"},
            None,
            worker_live=True,
        )
        == "running"
    )
    assert states.is_terminal_state("worker_stalled_candidate") is False


def test_watcher_loop_quiet_worktree_flags_despite_busy_canonical_root() -> None:
    """The tree-leg test the review required: per-dispatch cwd ≠ repo root.

    Sibling worktree writes keep the canonical root 'active'. Measuring the
    shared root would stay blind forever; measuring this dispatch's cwd flags.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        canonical, worker, src = _dual_tree_layout(tmp)
        tail = tmp / "worker.tail"
        tail.write_text("STATUS: investigating mid-senten", encoding="utf-8")
        now = time.time()
        os.utime(src, (now - 400.0, now - 400.0))
        os.utime(tail, (now - 400.0, now - 400.0))
        # Other worker is actively writing in a sibling tree under the root.
        os.utime(canonical / "worktrees" / "other-dispatch" / "busy.py", (now - 5.0, now - 5.0))
        _write_dispatch_record(
            tmp,
            dispatch_id=DISPATCH_ID,
            project_root=canonical,
            worker_cwd=worker,
        )
        status = tmp / "watcher.status.json"
        argv = [
            "goalflight_watch.py",
            "--pid",
            "424242",
            "--tail",
            str(tail),
            "--status-json",
            str(status),
            "--dispatch-id",
            DISPATCH_ID,
            "--poll-secs",
            str(POLL_SECS),
            "--max-idle-secs",
            "999999",
            "--wedge-idle-secs",
            "300",
            "--project-root",
            str(canonical),
            "--agent",
            "grok",
        ]

        def on_poll(index: int, tail_path: Path) -> None:
            if index == 2:
                tail_path.write_text(
                    "STATUS: investigating mid-sentence\n"
                    f"COMPLETE: {DISPATCH_ID} — recovered\n",
                    encoding="utf-8",
                )
                return
            if index > 6:
                raise AssertionError("watcher did not observe the terminal marker")

        rc, payloads, output = _run_wedge_watcher(
            argv,
            tmp,
            tree=worker,
            cpu_samples=[{424242: 12.0}, {424242: 12.0}, {424242: 12.0}, {424242: 12.0}],
            on_poll_sleep=on_poll,
        )
        wedged = [p for p in payloads if p.get("state") == "worker_stalled_candidate"]
        assert wedged, [p.get("state") for p in payloads]
        evidence = wedged[0].get("wedge_evidence") or {}
        tree_leg = wedged[0].get("wedge_tree_leg") or evidence
        scan_root = str(
            tree_leg.get("scan_root") or evidence.get("tree_scan_root") or ""
        )
        assert str(worker) in scan_root or Path(scan_root).resolve() == worker.resolve(), (
            scan_root,
            worker,
        )
        assert str(canonical) != scan_root
        assert "WATCHER-STALL-CANDIDATE " in output
        assert rc == 0


def test_watcher_loop_root_rooted_record_is_indeterminate() -> None:
    """cwd == canonical root: do not read other workers' writes as this life."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        canonical, _worker, src = _dual_tree_layout(tmp)
        # Root-rooted dispatch: the worker cwd IS the shared root.
        root_file = canonical / "app.py"
        root_file.write_text("print(1)\n", encoding="utf-8")
        tail = tmp / "worker.tail"
        tail.write_text("STATUS: maybe stuck\n", encoding="utf-8")
        now = time.time()
        os.utime(src, (now - 400.0, now - 400.0))
        os.utime(root_file, (now - 400.0, now - 400.0))
        os.utime(tail, (now - 400.0, now - 400.0))
        _write_dispatch_record(
            tmp,
            dispatch_id=DISPATCH_ID,
            project_root=canonical,
            worker_cwd=canonical,
        )
        status = tmp / "watcher.status.json"
        argv = [
            "goalflight_watch.py",
            "--pid",
            "424242",
            "--tail",
            str(tail),
            "--status-json",
            str(status),
            "--dispatch-id",
            DISPATCH_ID,
            "--poll-secs",
            str(POLL_SECS),
            "--max-idle-secs",
            "999999",
            "--wedge-idle-secs",
            "300",
            "--project-root",
            str(canonical),
            "--agent",
            "grok",
        ]

        def on_poll(index: int, tail_path: Path) -> None:
            if index == 2:
                tail_path.write_text(
                    "STATUS: maybe stuck\n"
                    f"COMPLETE: {DISPATCH_ID} — done\n",
                    encoding="utf-8",
                )
                return
            if index > 6:
                raise AssertionError("watcher did not observe the terminal marker")

        rc, payloads, output = _run_wedge_watcher(
            argv,
            tmp,
            tree=canonical,
            cpu_samples=[{424242: 4.0}, {424242: 4.0}, {424242: 4.0}],
            on_poll_sleep=on_poll,
        )
        assert all(p.get("state") != "worker_stalled_candidate" for p in payloads), [
            p.get("state") for p in payloads
        ]
        assert "WATCHER-STALL-CANDIDATE " not in output, output
        legs = [p.get("wedge_tree_leg") for p in payloads if p.get("wedge_tree_leg")]
        assert legs, "indeterminate tree leg must be named in the payload"
        assert legs[0].get("kind") == "indeterminate"
        assert rc == 0


def test_watcher_restart_does_not_reannounce_candidate() -> None:
    """P2: persist announcement in the status sidecar so a restart is silent."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        canonical, worker, src = _dual_tree_layout(tmp)
        tail = tmp / "worker.tail"
        tail.write_text("STATUS: investigating mid-senten", encoding="utf-8")
        now = time.time()
        os.utime(src, (now - 400.0, now - 400.0))
        os.utime(tail, (now - 400.0, now - 400.0))
        os.utime(canonical / "worktrees" / "other-dispatch" / "busy.py", (now - 5.0, now - 5.0))
        _write_dispatch_record(
            tmp,
            dispatch_id=DISPATCH_ID,
            project_root=canonical,
            worker_cwd=worker,
        )
        status = tmp / "watcher.status.json"
        status.write_text(
            json.dumps(
                {
                    "schema": "goalflight.status.v1",
                    "dispatch_id": DISPATCH_ID,
                    "state": "worker_stalled_candidate",
                    "reason": "worker_stalled_candidate",
                    "wedge_watch": {
                        "cputime_sample": {"424242": 12.0},
                        "cputime_sampled_at": now - 8.0,
                        "candidate_announced_at": now - 120.0,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        argv = [
            "goalflight_watch.py",
            "--pid",
            "424242",
            "--tail",
            str(tail),
            "--status-json",
            str(status),
            "--dispatch-id",
            DISPATCH_ID,
            "--poll-secs",
            str(POLL_SECS),
            "--max-idle-secs",
            "999999",
            "--wedge-idle-secs",
            "300",
            "--project-root",
            str(canonical),
            "--agent",
            "grok",
        ]

        def on_poll(index: int, tail_path: Path) -> None:
            if index == 2:
                tail_path.write_text(
                    "STATUS: investigating mid-sentence\n"
                    f"COMPLETE: {DISPATCH_ID} — recovered\n",
                    encoding="utf-8",
                )
                return
            if index > 6:
                raise AssertionError("watcher did not observe the terminal marker")

        rc, payloads, output = _run_wedge_watcher(
            argv,
            tmp,
            tree=worker,
            cpu_samples=[{424242: 12.0}, {424242: 12.0}, {424242: 12.0}],
            on_poll_sleep=on_poll,
        )
        assert "WATCHER-STALL-CANDIDATE " not in output, output
        assert any(p.get("state") == "worker_stalled_candidate" for p in payloads), [
            p.get("state") for p in payloads
        ]
        assert rc == 0


def test_watcher_spawn_argv_passes_worker_cwd_distinct_from_project_root() -> None:
    """Launcher wiring the review found untested: cwd is not the collapsed root."""
    import goalflight_dispatch as dispatch

    argv = dispatch._watcher_spawn_argv(
        worker_pid=1,
        tail=Path("/tmp/t"),
        status_json=Path("/tmp/s"),
        agent="grok",
        poll_secs=2.0,
        max_idle_secs=900.0,
        dispatch_id="d",
        pgid=1,
        project_root=Path("/repo"),
        worker_cwd=Path("/repo/worktrees/d"),
    )
    assert "--worker-cwd" in argv, argv
    assert argv[argv.index("--worker-cwd") + 1] == "/repo/worktrees/d"
    assert argv[argv.index("--project-root") + 1] == "/repo"
    assert argv[argv.index("--worker-cwd") + 1] != argv[argv.index("--project-root") + 1]


def test_watcher_restart_reuses_persisted_cpu_sample() -> None:
    """P2: last CPU sample lives in the sidecar, not only in RAM."""
    persisted = watch.load_wedge_watch_state(
        {
            "wedge_watch": {
                "cputime_sample": {"424242": 12.0},
                "cputime_sampled_at": 1_700_000_000.0,
                "candidate_announced_at": 1_700_000_090.0,
            }
        }
    )
    assert persisted["cputime_sample"] == {424242: 12.0}
    assert persisted["cputime_sampled_at"] == 1_700_000_000.0
    assert persisted["candidate_announced_at"] == 1_700_000_090.0
    dumped = watch.dump_wedge_watch_state(
        cputime_sample={424242: 12.5},
        cputime_sampled_at=1_700_000_010.0,
        candidate_announced_at=None,
    )
    assert dumped["cputime_sample"] == {"424242": 12.5}
    assert dumped["candidate_announced_at"] is None


def main() -> None:
    test_true_positive_all_three_legs_is_stall_candidate()
    test_default_sustain_is_fifteen_minutes_not_five()
    test_remote_wait_matches_all_three_legs_but_is_only_a_candidate()
    test_tail_bytes_grown_is_recorded_not_used_as_a_leg()
    test_false_positive_recent_tree_write_is_not_wedged()
    test_fresh_tail_is_not_wedged()
    test_cpu_moving_is_not_wedged()
    test_missing_cpu_delta_is_not_wedged()
    test_dead_process_is_not_worker_wedged()
    test_tree_mtime_walker_sees_recent_write()
    test_tree_mtime_walker_quiet_tree_is_old()
    test_git_dir_write_does_not_count_as_tree_activity()
    test_emit_once_on_enter_and_once_on_recover()
    test_emit_wedge_event_prints_once_shaped_record()
    test_stall_candidate_is_not_terminal_and_not_worker_dead()
    test_finish_ledger_refuses_to_terminalize_stall_candidate()
    test_apply_wedge_does_not_kill_or_terminal_write()
    test_status_overlay_surfaces_worker_wedged_without_terminalizing()
    test_watcher_loop_true_positive_emits_once_and_stays_alive()
    test_watcher_loop_recent_tree_write_never_wedges()
    test_cputime_delta_pairs_pids_instead_of_group_sum()
    test_tree_leg_scans_distinct_worker_cwd_not_canonical_root()
    test_tree_leg_indeterminate_when_cwd_is_canonical_root()
    test_tree_leg_indeterminate_when_worker_cwd_missing()
    test_chunk_summary_treats_stall_candidate_as_live_not_missing()
    test_watcher_loop_quiet_worktree_flags_despite_busy_canonical_root()
    test_watcher_loop_root_rooted_record_is_indeterminate()
    test_watcher_restart_does_not_reannounce_candidate()
    test_watcher_restart_reuses_persisted_cpu_sample()
    test_watcher_spawn_argv_passes_worker_cwd_distinct_from_project_root()
    print("OK: watch worker-wedge tests pass")


if __name__ == "__main__":
    main()
