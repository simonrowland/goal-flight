"""Tracked wires that close the grok rotation loop.

The rotator itself lives in the untracked ext zone. These cases lock the two
hooks the tracked side must fire, and that a missing extension is a no-op.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402
import goalflight_ledger as L  # noqa: E402
import goalflight_watch as W  # noqa: E402
import grok_seats  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_JOURNAL_DIR", str(tmp_path / "journals"))
    monkeypatch.setenv("GOALFLIGHT_WAKE_LEDGER_DIR", str(tmp_path / "wake"))
    monkeypatch.setenv("GOALFLIGHT_MESSAGES_DIR", str(tmp_path / "messages"))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", "/dev/null")
    grok_seats._RECOVER_CACHE = grok_seats._UNSET
    grok_seats._MARK_CACHE = grok_seats._UNSET
    yield
    grok_seats._RECOVER_CACHE = grok_seats._UNSET
    grok_seats._MARK_CACHE = grok_seats._UNSET


def _write_record(dispatch_id: str, **fields) -> Path:
    payload = {
        "schema": L.SCHEMA,
        "dispatch_id": dispatch_id,
        "agent": "grok-code",
        "engine": "grok",
        "shape": "bash",
        "account": "default",
        "effective_account": "seatA",
        "transport": "dispatch",
        "project_root": str(ROOT),
        "state": "running",
        "started_at": L.utc_now(),
    }
    payload.update(fields)
    path = L.record_path(dispatch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _stub_finish(monkeypatch: pytest.MonkeyPatch, module) -> None:
    def fake_finish(args):
        path = L.record_path(args.dispatch_id)
        if path.exists():
            record = json.loads(path.read_text(encoding="utf-8"))
        else:
            record = {"dispatch_id": args.dispatch_id}
        record["state"] = args.state
        record["terminal_state"] = args.state
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        return 0

    monkeypatch.setattr(module.goalflight_ledger, "cmd_finish", fake_finish)


def test_finish_ledger_marks_a_grok_quota_seat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marked: list[tuple[str, object]] = []
    monkeypatch.setattr(
        grok_seats,
        "note_exhausted",
        lambda seat, **kwargs: marked.append((seat, kwargs.get("path"))) or True,
    )
    _write_record("wire-quota")
    _stub_finish(monkeypatch, D)
    D._finish_ledger(
        "wire-quota",
        "quota_exhausted",
        {"limit_kind": "exhausted"},
        elapsed_s=1.0,
    )
    assert marked == [("seatA", None)], marked


def test_finish_ledger_does_not_mark_a_successful_or_foreign_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marked: list[str] = []
    monkeypatch.setattr(
        grok_seats,
        "note_exhausted",
        lambda seat, **kwargs: marked.append(seat) or True,
    )
    _stub_finish(monkeypatch, D)

    _write_record("wire-ok")
    D._finish_ledger("wire-ok", "complete", None, elapsed_s=1.0)

    _write_record(
        "wire-codex",
        agent="codex",
        engine="codex",
        effective_account="seatA",
    )
    D._finish_ledger(
        "wire-codex",
        "quota_exhausted",
        {"limit_kind": "exhausted"},
        elapsed_s=1.0,
    )

    _write_record("wire-host", effective_account="")
    D._finish_ledger(
        "wire-host",
        "quota_exhausted",
        {"limit_kind": "exhausted"},
        elapsed_s=1.0,
    )
    assert marked == []


def test_watch_finish_marks_a_grok_quota_seat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Background dispatches finish in the watcher, not dispatch._finish_ledger."""
    marked: list[str] = []
    monkeypatch.setattr(
        grok_seats,
        "note_exhausted",
        lambda seat, **kwargs: marked.append(seat) or True,
    )
    _write_record("wire-watch")
    _stub_finish(monkeypatch, W)
    assert (
        W._finish_existing_ledger(
            "wire-watch",
            "quota_exhausted",
            {"limit_kind": "exhausted"},
            agent="grok-code",
        )
        is None
    )
    assert marked == ["seatA"], marked


def test_missing_rotator_does_not_fail_finish_or_watch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grok_seats._MARK_CACHE = None
    monkeypatch.setattr(grok_seats, "_optional_mark_exhausted", lambda: None)
    _write_record("wire-absent")
    _stub_finish(monkeypatch, D)
    D._finish_ledger(
        "wire-absent",
        "quota_exhausted",
        {"limit_kind": "exhausted"},
        elapsed_s=1.0,
    )
    _stub_finish(monkeypatch, W)
    assert (
        W._finish_existing_ledger(
            "wire-absent",
            "quota_exhausted",
            {"limit_kind": "exhausted"},
            agent="grok-code",
        )
        is None
    )
    D._maybe_mark_grok_quota_exhausted("wire-absent", "quota_exhausted")
    W._maybe_note_grok_quota("wire-absent", "quota_exhausted")


def test_helper_swallows_a_rotator_that_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError("rotator exploded")

    monkeypatch.setattr(grok_seats, "note_exhausted_if_proven", boom)
    _write_record("wire-boom")
    D._maybe_mark_grok_quota_exhausted("wire-boom", "quota_exhausted")
    W._maybe_note_grok_quota("wire-boom", "quota_exhausted")


def test_next_select_routes_away_after_a_proven_quota(
    tmp_path: Path,
) -> None:
    """Verify step 2: the mark is visible to the next selection, not the TTL."""
    path = tmp_path / "grok-seat-states.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": 1_786_000_000.0,
                "seats": {
                    "": {"ok": True, "used_percent": 100.0, "error": None},
                    "seatA": {"ok": True, "used_percent": 3.0, "error": None},
                },
            }
        ),
        encoding="utf-8",
    )
    assert grok_seats.select_seat(path=path, now=1_786_000_000.0, allow_refresh=False) == "seatA"

    def marker(seat, **kwargs):
        document = json.loads(path.read_text(encoding="utf-8"))
        document["seats"][seat] = {
            "ok": True,
            "used_percent": float(grok_seats.EXHAUSTED_AT_PERCENT),
            "error": "quota_exhausted (observed)",
        }
        path.write_text(json.dumps(document), encoding="utf-8")

    record = {
        "engine": "grok",
        "agent": "grok-code",
        "state": "quota_exhausted",
        "effective_account": "seatA",
    }
    assert grok_seats.note_exhausted_if_proven(record, path=path, marker=marker)
    assert (
        grok_seats.select_seat(path=path, now=1_786_000_000.0, allow_refresh=False)
        is None
    )


def test_refresh_default_path_uses_the_optional_recover_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify step 1: tracked refresh recovers a 401 without an operator step."""
    monkeypatch.setattr(
        grok_seats.grok_usage,
        "accounts",
        lambda: [("seatA", Path("/seat/auth.json"))],
    )
    probes = iter(
        [
            {"ok": False, "error": "billing endpoint returned HTTP 401"},
            {"ok": True, "used_percent": 8.0, "error": None},
        ]
    )
    recover_calls: list[object] = []

    def reader(*, auth_path, timeout_s, account):
        return next(probes)

    def hook(**kwargs):
        recover_calls.append(kwargs)
        return kwargs["reader"](
            auth_path=kwargs["auth_path"],
            timeout_s=kwargs["timeout_s"],
            account=kwargs["label"],
        )

    monkeypatch.setattr(grok_seats, "_optional_recover", lambda: hook)
    path = tmp_path / "s.json"
    document = grok_seats.refresh_states(path=path, now=1_786_000_000.0, reader=reader)
    assert recover_calls, "default refresh must load the optional recover hook"
    assert document["seats"]["seatA"]["ok"] is True
    assert grok_seats.select_seat(path=path, now=1_786_000_000.0, allow_refresh=False) == "seatA"


def test_installed_rotator_recovers_401_without_a_hand_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end with the local rotator when this checkout has one."""
    try:
        from ext import grok_rotate
    except BaseException:
        pytest.skip("local rotator is not installed")
    if not callable(getattr(grok_rotate, "recover_probe", None)):
        pytest.skip("local rotator has no recover_probe hook")

    monkeypatch.setattr(
        grok_seats.grok_usage,
        "accounts",
        lambda: [("seatA", tmp_path / "seatA" / ".grok" / "auth.json")],
    )
    probes = iter(
        [
            {"ok": False, "error": "billing endpoint returned HTTP 401"},
            {"ok": True, "used_percent": 8.0, "error": None},
        ]
    )
    nudged: list[Path] = []

    def reader(*, auth_path, timeout_s, account):
        return next(probes)

    def recover(**kwargs):
        return grok_rotate.recover_probe(
            nudge=lambda home: nudged.append(home) or True,
            **kwargs,
        )

    grok_seats._RECOVER_CACHE = recover
    path = tmp_path / "s.json"
    document = grok_seats.refresh_states(path=path, now=1_786_000_000.0, reader=reader)
    assert nudged, "the installed rotator must nudge on a 401"
    assert document["seats"]["seatA"]["ok"] is True, document


def test_installed_rotator_mark_routes_the_next_select(
    tmp_path: Path,
) -> None:
    try:
        from ext import grok_rotate
    except BaseException:
        pytest.skip("local rotator is not installed")

    path = tmp_path / "grok-seat-states.json"
    grok_rotate.mark_exhausted  # attribute must exist
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": 1_786_000_000.0,
                "seats": {
                    "": {"ok": True, "used_percent": 100.0, "error": None},
                    "seatA": {"ok": True, "used_percent": 3.0, "error": None},
                },
            }
        ),
        encoding="utf-8",
    )
    assert grok_seats.select_seat(path=path, now=1_786_000_000.0, allow_refresh=False) == "seatA"
    grok_rotate.mark_exhausted("seatA", path=path)
    assert (
        grok_seats.select_seat(path=path, now=1_786_000_000.0, allow_refresh=False)
        is None
    )


def test_terminal_writers_keep_the_mark_helper_wired() -> None:
    """Drain and finish paths must actually call the helper; a rename-only is not enough."""
    dispatch_src = (ROOT / "scripts" / "goalflight_dispatch.py").read_text(
        encoding="utf-8"
    )
    watch_src = (ROOT / "scripts" / "goalflight_watch.py").read_text(encoding="utf-8")
    assert dispatch_src.count("_maybe_mark_grok_quota_exhausted(") >= 5
    assert "def _finish_ledger" in dispatch_src
    finish_idx = dispatch_src.index("def _finish_ledger")
    next_def = dispatch_src.find("\ndef ", finish_idx + 1)
    finish_body = dispatch_src[finish_idx:next_def]
    assert "_maybe_mark_grok_quota_exhausted" in finish_body
    assert "_maybe_note_grok_quota(dispatch_id, state)" in watch_src


