#!/usr/bin/env python3
"""Trace-liveness must cover every engine, not just the two it knew (t-186).

The classifier's rule is that a measured activity signal vetoes a wedge, and
that a probe which cannot answer is unknown rather than evidence of death. The
trace channel implements that for a worker whose console is silent while it is
plainly working -- thinking for minutes, or streaming into a buffered pipe.

Two gaps meant whole engines never got that veto:

1. `_known_trace_roots` listed only ~/.codex/sessions and ~/.kimi-code/sessions.
   grok and cursor had NO root, so their traces were unrecognisable even when
   found.
2. Resolution fell back to lsof against the tracked pid, which only sees a file
   that pid holds OPEN. An engine that appends and closes per turn, or whose
   real work runs in a process the watcher is not tracking, resolves to
   nothing -- so the tail was its only signal.

The path is derivable from engine + worker cwd, so it is now derived.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_watch as W  # noqa: E402


CWD = "/Users/x/Repos/proj/worktrees/label/s-1"
DEAD_PID = 999999  # deliberately not running: lsof must be irrelevant


def _seed(home: Path, engine: str, *, age_s: float) -> Path:
    """Write a trace where `engine` really keeps one, aged `age_s`."""
    if engine == "cursor":
        enc = CWD.lstrip("/").replace("/", "-")
        d = home / ".cursor" / "projects" / enc / "agent-transcripts" / "sess-1"
    elif engine == "grok":
        import urllib.parse

        d = home / ".grok" / "sessions" / urllib.parse.quote(CWD, safe="") / "sess-1"
    else:
        raise AssertionError(engine)
    d.mkdir(parents=True)
    f = d / "trace.jsonl"
    f.write_text('{"turn":1}\n')
    stamp = time.time() - age_s
    import os

    os.utime(f, (stamp, stamp))
    os.utime(d, (stamp, stamp))
    return f


def _sample(home: Path, engine: str, *, idle_threshold: float = 900.0) -> dict:
    tl = W.TraceLiveness(
        dispatch_id=None,
        worker_pid=DEAD_PID,
        engine=engine,
        worker_cwd=CWD,
        home=home,
    )
    return tl.sample(
        now_epoch=time.time(),
        now_mono=W.active_monotonic(),
        idle_threshold=idle_threshold,
    )


@pytest.mark.parametrize("engine", ["cursor", "grok"])
def test_a_fresh_trace_is_active_without_any_open_file_descriptor(
    tmp_path: Path, engine: str
) -> None:
    """The worker pid is dead here; resolution must not depend on lsof."""
    home = tmp_path / "home"
    _seed(home, engine, age_s=30.0)
    got = _sample(home, engine)
    assert got.get("trace_path"), f"{engine} did not resolve: {got}"
    assert got["trace_active"] is True, got
    assert W._trace_vetoes_idle(trace_active=got["trace_active"]) is True


@pytest.mark.parametrize("engine", ["cursor", "grok"])
def test_a_stale_trace_does_not_veto(tmp_path: Path, engine: str) -> None:
    """The veto must stay evidence, not a blanket exemption for these engines."""
    home = tmp_path / "home"
    _seed(home, engine, age_s=10_000.0)
    got = _sample(home, engine)
    assert got.get("trace_path"), got
    assert got["trace_active"] is False, got
    assert W._trace_vetoes_idle(trace_active=got["trace_active"]) is False


def test_another_workers_trace_is_not_this_workers_evidence(tmp_path: Path) -> None:
    """Scoped to cwd: one worker's activity must not vouch for another's."""
    home = tmp_path / "home"
    _seed(home, "cursor", age_s=5.0)          # belongs to CWD
    tl = W.TraceLiveness(
        dispatch_id=None,
        worker_pid=DEAD_PID,
        engine="cursor",
        worker_cwd="/Users/x/Repos/proj/worktrees/label/s-9",   # a different seat
        home=home,
    )
    got = tl.sample(
        now_epoch=time.time(), now_mono=W.active_monotonic(), idle_threshold=900.0
    )
    assert not got.get("trace_path"), f"resolved a foreign worker's trace: {got}"


def test_an_engine_with_no_known_layout_stays_unknown(tmp_path: Path) -> None:
    """Unknown must not collapse into 'inactive', which reads as death."""
    got = _sample(tmp_path / "home", "claude")
    assert got == {} or not got.get("trace_path"), got
    assert "trace_active" not in got, "absence of a layout is not a measurement"


def test_the_known_roots_cover_every_engine_that_keeps_a_trace(tmp_path: Path) -> None:
    roots = W._known_trace_roots(state_dir=tmp_path / "state", home=tmp_path / "home")
    joined = " ".join(str(r) for r in roots)
    for expected in (".codex/sessions", ".kimi-code/sessions",
                     ".grok/sessions", ".cursor/projects"):
        assert expected in joined, f"{expected} missing from {joined}"


# --------------------------------------------------------------------------
# false ALIVE: the inverse failure, found by review of 0043a20
#
# Worktree seats are POOLED. A previous dispatch in the same seat leaves its
# trace in the same directory, and while that trace is younger than the idle
# window it would vouch for a worker that has done nothing. That is worse than
# the false death this channel exists to prevent, because a false death
# eventually resolves and a false alive never does.
# --------------------------------------------------------------------------


def _sample_started(home: Path, engine: str, started_epoch: float) -> dict:
    tl = W.TraceLiveness(
        dispatch_id=None,
        worker_pid=DEAD_PID,
        engine=engine,
        worker_cwd=CWD,
        home=home,
        started_epoch=started_epoch,
    )
    return tl.sample(
        now_epoch=time.time(), now_mono=W.active_monotonic(), idle_threshold=900.0
    )


@pytest.mark.parametrize("engine", ["cursor", "grok"])
def test_a_predecessors_trace_in_a_pooled_seat_does_not_vouch(
    tmp_path: Path, engine: str
) -> None:
    """Recent, but written before THIS dispatch began: not our evidence."""
    home = tmp_path / "home"
    _seed(home, engine, age_s=120.0)             # a prior dispatch, 2 min ago
    got = _sample_started(home, engine, started_epoch=time.time() - 60.0)
    assert got.get("trace_path"), "it should still resolve, just not vouch"
    assert got["trace_active"] is False, got
    assert W._trace_vetoes_idle(trace_active=got["trace_active"]) is False


@pytest.mark.parametrize("engine", ["cursor", "grok"])
def test_a_trace_written_during_this_dispatch_still_vouches(
    tmp_path: Path, engine: str
) -> None:
    """The fix must not silence the signal it exists to provide."""
    home = tmp_path / "home"
    _seed(home, engine, age_s=30.0)
    got = _sample_started(home, engine, started_epoch=time.time() - 600.0)
    assert got["trace_active"] is True, got


def test_without_a_start_bound_the_old_permissive_behaviour_remains(
    tmp_path: Path,
) -> None:
    """started_epoch=None must stay usable rather than fail closed.

    Callers that cannot say when the dispatch began should not lose the channel
    entirely; they simply get the weaker recency test.
    """
    home = tmp_path / "home"
    _seed(home, "cursor", age_s=120.0)
    got = _sample_started(home, "cursor", started_epoch=None)
    assert got["trace_active"] is True, got


def test_claude_has_a_layout_too_with_its_own_encoding(tmp_path: Path) -> None:
    """Found by a second reviewer that the first missed.

    claude keeps per-cwd traces like cursor, but encodes the leading slash as
    a dash instead of stripping it. Copying cursor's rule would silently
    resolve nothing, which reads as 'no trace' rather than 'wrong path'.
    """
    import goalflight_engine_sessions as E

    home = tmp_path / "home"
    d = home / ".claude" / "projects" / CWD.replace("/", "-")
    d.mkdir(parents=True)
    f = d / "sess.jsonl"
    f.write_text('{"turn":1}\n')

    dirs = E.session_trace_dirs("claude", home=home, worker_cwd=CWD)
    assert dirs and dirs[0].name.startswith("-"), dirs
    assert E.session_trace_newest_mtime("claude", home=home, worker_cwd=CWD) is not None

    # the cursor encoding must NOT resolve claude's tree
    wrong = home / ".claude" / "projects" / CWD.lstrip("/").replace("/", "-")
    assert not wrong.exists(), "fixture sanity"
