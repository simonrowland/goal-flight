"""A resume must be able to change seats, because the seat is why it stopped.

Grok/cursor keep a session as FILES under the billing seat's HOME
(``<account-home>/.grok/sessions/<quote(cwd)>/<session-id>/``). Landing a resume
on a different seat without those files makes the CLI fall back to its
per-account REMOTE registry, which 404s, and the worker's whole accumulated
context is lost -- so the operator pays for a fresh redispatch anyway.

Observed 2026-09-06 in battery-tool-v2: parent on seat ``gmail`` with session
``c751a12b...`` was resumed with ``--account rpp`` and died
``Failed to restore session from remote: ... 404 Not Found``. The quota wall is
precisely the case that needs a different seat, so "resume on the same seat" is
not an answer.

These pin the migration that makes the seat change work.
"""

from __future__ import annotations

import importlib.util
import sys
import urllib.parse
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_dispatch():
    spec = importlib.util.spec_from_file_location(
        "gfd_migration", REPO_ROOT / "scripts" / "goalflight_dispatch.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclasses resolve their module from sys.modules during class creation.
    sys.modules["gfd_migration"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop("gfd_migration", None)
    return module


@pytest.fixture(scope="module")
def gfd():
    return _load_dispatch()


CWD = "/Users/x/Repos/proj/worktrees/label/s-7"
SID = "c751a12b-7645-4f72-9780-6017f6594b3d"


def _seed(gfd, monkeypatch, tmp_path: Path, account: str) -> Path:
    """Build a realistic session dir for `account` under a fake home."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    session = gfd._seat_session_dir(account, "grok", CWD, SID)
    session.mkdir(parents=True)
    (session / "chat_history.jsonl").write_text('{"role":"user"}\n')
    (session / "summary.json").write_text('{"title":"t"}')
    (session / "system_prompt.txt").write_text("sys")
    (session / "chat_history.jsonl.lock").write_text("")
    (session / "summary.json.lock").write_text("")
    nested = session / "terminal"
    nested.mkdir()
    (nested / "0.json").write_text("{}")
    return session


def test_session_path_uses_url_encoded_cwd(gfd, monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    got = gfd._seat_session_dir("gmail", "grok", CWD, SID)
    assert got.name == SID
    assert got.parent.name == urllib.parse.quote(CWD, safe="")
    assert ".grok/sessions" in str(got)


def test_migration_moves_the_session_to_the_healthy_seat(gfd, monkeypatch, tmp_path):
    src = _seed(gfd, monkeypatch, tmp_path, "gmail")
    ok, detail = gfd.migrate_seat_session(
        engine="grok", session_id=SID, worker_cwd=CWD,
        from_account="gmail", to_account="rpp",
    )
    assert ok, detail
    dst = gfd._seat_session_dir("rpp", "grok", CWD, SID)
    assert dst.is_dir()
    assert (dst / "chat_history.jsonl").read_text() == '{"role":"user"}\n'
    assert (dst / "terminal" / "0.json").is_file(), "nested dirs must travel"
    assert src.is_dir(), "the owning seat keeps its copy; migration is not a move"


def test_migration_does_not_carry_the_dead_process_locks(gfd, monkeypatch, tmp_path):
    _seed(gfd, monkeypatch, tmp_path, "gmail")
    gfd.migrate_seat_session(
        engine="grok", session_id=SID, worker_cwd=CWD,
        from_account="gmail", to_account="rpp",
    )
    dst = gfd._seat_session_dir("rpp", "grok", CWD, SID)
    locks = sorted(p.name for p in dst.iterdir() if p.name.endswith(".lock"))
    assert locks == [], f"stale locks belong to the dead pid: {locks}"


def test_migration_is_idempotent(gfd, monkeypatch, tmp_path):
    _seed(gfd, monkeypatch, tmp_path, "gmail")
    first = gfd.migrate_seat_session(
        engine="grok", session_id=SID, worker_cwd=CWD,
        from_account="gmail", to_account="rpp",
    )
    second = gfd.migrate_seat_session(
        engine="grok", session_id=SID, worker_cwd=CWD,
        from_account="gmail", to_account="rpp",
    )
    assert first[0] and second[0], (first, second)
    assert "already present" in second[1]


def test_same_seat_is_a_noop(gfd, monkeypatch, tmp_path):
    _seed(gfd, monkeypatch, tmp_path, "gmail")
    ok, detail = gfd.migrate_seat_session(
        engine="grok", session_id=SID, worker_cwd=CWD,
        from_account="gmail", to_account="gmail",
    )
    assert ok and "no migration needed" in detail


def test_absent_source_reports_failure_rather_than_pretending(gfd, monkeypatch, tmp_path):
    """The caller refuses on this; it must never look like a success."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    ok, detail = gfd.migrate_seat_session(
        engine="grok", session_id=SID, worker_cwd=CWD,
        from_account="gmail", to_account="rpp",
    )
    assert ok is False
    assert "not present on the owning seat" in detail
    assert not gfd._seat_session_dir("rpp", "grok", CWD, SID).exists()


def test_unknown_engine_is_reported_not_guessed(gfd, monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    ok, detail = gfd.migrate_seat_session(
        engine="codex", session_id=SID, worker_cwd=CWD,
        from_account="gmail", to_account="rpp",
    )
    assert ok is False
    assert "no known seat-scoped session layout" in detail


def test_refusal_when_no_seat_has_tokens_names_the_real_options(gfd):
    """When no seat has tokens, resume must refuse and say what to do.

    Grok meters a SHARED "Build usage balance", so a seat change buys nothing
    once it is gone: launching anyway spends a dispatch to rediscover the same
    402 and re-terminalizes the parent. Observed 2026-09-06 with all four
    configured grok seats walled at once.
    """
    msg = gfd.no_healthy_seat_message("grok", "gmail", ["gmail", "info", "rpp", "simon"])
    assert "'gmail'" in msg, "must name the owning seat"
    for seat in ("info", "rpp", "simon"):
        assert seat in msg, f"must name the alternatives it checked ({seat})"
    assert "goalflight_usage.py" in msg, "must point at the reset time"
    assert "redispatch" in msg, "must offer the other way out"
    assert "nothing was lost" in msg, "must say the session survives"


def test_refusal_handles_a_fleet_with_no_configured_seats(gfd):
    msg = gfd.no_healthy_seat_message("grok", "gmail", [])
    assert "none configured" in msg
