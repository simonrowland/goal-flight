#!/usr/bin/env python3
"""A worker that left work behind must not be scored the same as one that did nothing.

A terminal marker is accepted only when it carries the dispatch id. That rule
is deliberate and stays: a generic marker from any process could otherwise
terminalize someone else's dispatch.

But the two failures were being reported identically. Measured 2026-09-06:
cursor workers emit `!COMPLETE: <summary>` with no id, while moonshot emits the
prefixed form from the SAME prompt file -- so every cursor dispatch was scored
`death_cause=no_evidence`, indistinguishable from a worker that produced
nothing, and its seat was recycled. One operator lost a finished commit of 11
files that way, and reported the worker had been "vindicated completely".

`no_evidence` now means we also looked in the seat and found none.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_watch as W  # noqa: E402


def _seat(tmp_path: Path) -> Path:
    seat = tmp_path / "seat"
    seat.mkdir()
    subprocess.run(["git", "init", "-q", str(seat)], check=True)
    (seat / "base.txt").write_text("base")
    subprocess.run(["git", "-C", str(seat), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(seat), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "base"],
        check=True, capture_output=True,
    )
    return seat


def _reason(seat: Path | None, started: float | None) -> str:
    """The postmortem cause for a dead worker that emitted no accepted marker."""
    return W._worker_dead_no_marker_reason(
        Path("/nonexistent-tail"),
        prompt_provenance_available=False,   # forces the no-marker branch
        worker_cwd=seat,
        started_epoch=started,
    )


def test_uncommitted_work_in_the_seat_is_not_no_evidence(tmp_path: Path) -> None:
    seat = _seat(tmp_path)
    (seat / "wip.txt").write_text("the worker's output")
    got = _reason(seat, time.time() - 60)
    assert got.endswith("unharvested_work_dirty"), got


def test_a_commit_made_during_the_dispatch_is_not_no_evidence(tmp_path: Path) -> None:
    """kiln's case: a finished commit sitting in a seat reaped as no_evidence."""
    seat = _seat(tmp_path)
    started = time.time() - 60
    (seat / "done.txt").write_text("finished work")
    subprocess.run(["git", "-C", str(seat), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(seat), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "the work"],
        check=True, capture_output=True,
    )
    got = _reason(seat, started)
    assert got.endswith("unharvested_work_committed"), got


def test_a_genuinely_empty_seat_still_reports_no_evidence(tmp_path: Path) -> None:
    """The label must keep its meaning, or it stops being worth anything."""
    seat = _seat(tmp_path)
    got = _reason(seat, time.time() + 3600)  # every commit predates the dispatch
    assert got.endswith("death_cause=no_evidence"), got


def test_an_unreadable_worker_directory_is_reported_as_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seat = _seat(tmp_path)
    real_is_dir = Path.is_dir

    def unreadable(path: Path) -> bool:
        if path == seat:
            raise PermissionError("injected unreadable worker directory")
        return real_is_dir(path)

    monkeypatch.setattr(Path, "is_dir", unreadable)
    got = _reason(seat, time.time() - 60)
    assert got.endswith("death_cause=worker_cwd_unavailable"), got


def test_a_failed_git_probe_is_not_reported_as_absent_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seat = _seat(tmp_path)
    real_run = subprocess.run

    def failed_status(command, *args, **kwargs):
        if command[:4] == ["git", "-C", str(seat), "status"]:
            return subprocess.CompletedProcess(command, 128, "", "injected probe failure")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(W.subprocess, "run", failed_status)
    got = _reason(seat, time.time() - 60)
    assert got.endswith("death_cause=worktree_probe_failed"), got


def test_unknown_tail_classification_still_checks_the_worker_seat(tmp_path: Path) -> None:
    seat = _seat(tmp_path)
    (seat / "wip.txt").write_text("the worker's output")
    tail = tmp_path / "tail.txt"
    tail.write_text("worker stopped without a diagnostic\n", encoding="utf-8")

    got = W._worker_dead_no_marker_reason(
        tail,
        [],
        prompt_provenance_available=True,
        worker_cwd=seat,
        started_epoch=time.time() - 60,
    )
    assert got.endswith("death_cause=unharvested_work_dirty"), got


@pytest.mark.parametrize(
    "label, seat_factory",
    [
        ("no cwd recorded", lambda tmp: None),
        ("cwd is not a git tree", lambda tmp: tmp / "plain"),
    ],
)
def test_an_unanswerable_probe_does_not_invent_work(
    tmp_path: Path, label: str, seat_factory
) -> None:
    """Missing cwd is invalid input; a failed Git probe is not work evidence."""
    seat = seat_factory(tmp_path)
    if seat is not None:
        seat.mkdir()
    if label == "no cwd recorded":
        with pytest.raises(ValueError, match="dispatch record is missing worker_cwd"):
            W.worktree_work_evidence(seat, since_epoch=time.time() - 60)
        return
    got = _reason(seat, time.time() - 60)
    expected_cause = (
        "worktree_probe_failed"
        if label == "cwd is not a git tree"
        else "no_evidence"
    )
    assert got.endswith(f"death_cause={expected_cause}"), f"{label}: {got}"


def test_the_verdict_is_still_worker_dead_and_the_marker_rule_is_untouched(
    tmp_path: Path,
) -> None:
    """Evidence changes the CAUSE, never the verdict, and never marker acceptance.

    Relaxing the id requirement would reopen the cross-dispatch poisoning it
    exists to prevent, so this pins that the seat probe did not become a
    backdoor success path.
    """
    seat = _seat(tmp_path)
    (seat / "wip.txt").write_text("work")
    got = _reason(seat, time.time() - 60)
    assert got.startswith("worker_dead_no_terminal_marker:"), got
    assert "COMPLETE" not in got and "complete" not in got, got


# --------------------------------------------------------------------------
# the operator surface: knowing WHICH seat to open
# --------------------------------------------------------------------------

import goalflight_status as S  # noqa: E402


def test_the_row_names_the_seat_and_the_command() -> None:
    """The capability belongs at the decision point, not in a runbook."""
    hint = S._harvest_hint({
        "reason": "worker_dead_no_terminal_marker:death_cause=unharvested_work_committed",
        "wedge_tree_leg": {"scan_root": "/repo/worktrees/x/s-5"},
    })
    assert "UNHARVESTED WORK" in hint and "a commit" in hint, hint
    assert "/repo/worktrees/x/s-5" in hint, hint
    assert "git -C /repo/worktrees/x/s-5" in hint, "must be runnable as printed"


def test_uncommitted_work_is_described_as_such() -> None:
    hint = S._harvest_hint({
        "reason": "worker_dead_no_terminal_marker:death_cause=unharvested_work_dirty",
        "wedge_tree_leg": {"worker_cwd": "/repo/worktrees/x/s-5"},
    })
    assert "uncommitted changes" in hint, hint


def test_a_missing_seat_path_says_so_rather_than_printing_a_broken_command() -> None:
    hint = S._harvest_hint({
        "reason": "worker_dead_no_terminal_marker:death_cause=unharvested_work_dirty",
    })
    assert "worktree path not recorded" in hint, hint
    assert "git -C" not in hint, "never print a command with a hole in it"


def test_status_worktree_path_drives_salvage_hint_and_missing_path_refuses() -> None:
    status = {
        "reason": "worker_dead_no_terminal_marker:death_cause=unharvested_work_dirty",
        "worktree_seat": "s-5",
        "worktree_path": "/repo/worktrees/x/s-5",
    }
    hint = S._harvest_hint(status)
    assert "git -C /repo/worktrees/x/s-5" in hint, hint

    status["worktree_path"] = ""
    missing = S._harvest_hint(status)
    assert "worktree path not recorded" in missing, missing
    assert "git -C" not in missing, "an empty recorded path must not target cwd"


def test_an_ordinary_dead_dispatch_gets_no_hint() -> None:
    """A hint on every row is a hint nobody reads."""
    assert S._harvest_hint({
        "reason": "worker_dead_no_terminal_marker:death_cause=no_evidence",
        "wedge_tree_leg": {"scan_root": "/repo/worktrees/x/s-5"},
    }) == ""
    assert S._harvest_hint({"reason": "marker:COMPLETE"}) == ""
