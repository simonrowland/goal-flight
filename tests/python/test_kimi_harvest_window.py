"""Moonshot harvest must bound a pooled seat to this dispatch's start."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_ledger  # noqa: E402
import goalflight_watch as watch  # noqa: E402


STARTED = 1_700_000_000.0
OLD_HANDLE = "session_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
NEW_HANDLE = "session_bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
LATER_HANDLE = "session_cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat()


def _plant_sessions(home: Path, work: Path, stamps: list[tuple[str, float]]) -> None:
    index = home / ".kimi-code" / "session_index.jsonl"
    index.parent.mkdir(parents=True)
    rows = []
    for handle, mtime in stamps:
        session_dir = home / "sessions" / handle
        session_dir.mkdir(parents=True)
        os.utime(session_dir, (mtime, mtime))
        rows.append(
            {
                "sessionId": handle,
                "workDir": str(work),
                "sessionDir": str(session_dir),
            }
        )
    index.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _run_watch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    home: Path,
    work: Path,
    dispatch_id: str,
) -> dict:
    tail = tmp_path / "tail.txt"
    tail.write_text("", encoding="utf-8")
    status = tmp_path / "status.json"
    project = tmp_path / "project"
    project.mkdir()
    record = goalflight_ledger.record_path(dispatch_id)
    record.write_text(
        json.dumps({"dispatch_id": dispatch_id, "started_at": _iso(STARTED)}),
        encoding="utf-8",
    )
    clock = {"now": 0.0}
    sleeps = {"n": 0}

    def fake_monotonic() -> float:
        return clock["now"]

    def controlled_sleep(_seconds: float) -> None:
        sleeps["n"] += 1
        if sleeps["n"] > 4:
            raise AssertionError("watcher did not exit")
        clock["now"] += 10.0

    monkeypatch.setattr(watch.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(watch, "active_monotonic", fake_monotonic)
    monkeypatch.setattr(watch.time, "sleep", controlled_sleep)
    monkeypatch.setattr(watch.atexit, "register", lambda _callback: None)
    monkeypatch.setattr(watch.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(watch, "worker_alive", lambda _pid, _identity: (False, "dead", None))
    monkeypatch.setattr(watch, "process_group_id", lambda pid: pid)
    monkeypatch.setattr(watch, "pgroup_cpu_pct", lambda _pgid: 0.0)
    monkeypatch.setattr(watch, "live_descendant_count", lambda _pid: 0)
    monkeypatch.setattr(watch, "system_starved", lambda: False)
    monkeypatch.setattr(watch.TraceLiveness, "sample", lambda self, **_kwargs: {})
    monkeypatch.setattr(
        watch,
        "sample_newest_mtime_under",
        lambda *_args, **_kwargs: watch.TreeMtimeSample(newest=0.0, available=True),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "goalflight_watch.py",
            "--pid",
            "424242",
            "--tail",
            str(tail),
            "--status-json",
            str(status),
            "--dispatch-id",
            dispatch_id,
            "--project-root",
            str(project),
            "--worker-cwd",
            str(work),
            "--poll-secs",
            "0.05",
            "--max-idle-secs",
            "30",
            "--wedge-idle-secs",
            "0",
            "--agent",
            "moonshot",
            "--detached",
        ],
    )
    watch.main()
    return json.loads(status.read_text(encoding="utf-8"))


def test_kimi_harvest_keeps_the_session_after_dispatch_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    work = tmp_path / "seat"
    work.mkdir()
    _plant_sessions(
        home,
        work,
        [(OLD_HANDLE, STARTED - 100.0), (NEW_HANDLE, STARTED + 30.0)],
    )
    status = _run_watch(
        monkeypatch,
        tmp_path,
        home=home,
        work=work,
        dispatch_id="kimi-window-one",
    )
    assert status.get("engine_session_id") == NEW_HANDLE


def test_kimi_harvest_stays_ambiguous_when_two_sessions_follow_start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Control: two sessions after the dispatch start are still not a guess."""
    home = tmp_path / "home"
    work = tmp_path / "seat"
    work.mkdir()
    _plant_sessions(
        home,
        work,
        [(NEW_HANDLE, STARTED + 10.0), (LATER_HANDLE, STARTED + 40.0)],
    )
    status = _run_watch(
        monkeypatch,
        tmp_path,
        home=home,
        work=work,
        dispatch_id="kimi-window-two",
    )
    assert status.get("engine_session_id") is None
