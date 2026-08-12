"""Hermetic tests for grok seat selection.

No network and no real auth documents: the probe reader is injected.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "grok_seats.py"
SPEC = importlib.util.spec_from_file_location("test_target_grok_seats", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
seats = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = seats
SPEC.loader.exec_module(seats)

NOW = 1_786_000_000.0


def _states(path: Path, mapping: dict, *, updated_at: float = NOW) -> Path:
    path.write_text(
        json.dumps({"version": 1, "updated_at": updated_at, "seats": mapping})
    )
    return path


def _entry(used, ok=True):
    return {"ok": ok, "used_percent": used, "error": None if ok else "probe failed"}


@pytest.mark.parametrize(
    ("used", "eligible"),
    [(0.0, True), (50.0, True), (94.9, True), (95.0, False), (99.0, False), (100.0, False)],
)
def test_flip_threshold_is_95_percent_used(tmp_path: Path, used, eligible) -> None:
    """A seat is starved at 95% spent, not at 100%.

    Waiting for 100 means flipping only after dispatches have already started
    failing; the flip has to happen while there is still headroom to flip with.
    """
    path = _states(tmp_path / "s.json", {"": _entry(100.0), "seat": _entry(used)})
    selected = seats.select_seat(path=path, now=NOW, allow_refresh=False)
    assert (selected == "seat") is eligible


def test_starved_host_flips_to_a_seat_with_headroom(tmp_path: Path) -> None:
    path = _states(tmp_path / "s.json", {"": _entry(100.0), "seat": _entry(10.0)})
    assert seats.select_seat(path=path, now=NOW, allow_refresh=False) == "seat"


def test_host_is_chosen_by_returning_none(tmp_path: Path) -> None:
    """None means 'use the host default' -- the same action as no opinion."""
    path = _states(tmp_path / "s.json", {"": _entry(5.0), "seat": _entry(60.0)})
    assert seats.select_seat(path=path, now=NOW, allow_refresh=False) is None


def test_least_used_seat_wins(tmp_path: Path) -> None:
    path = _states(
        tmp_path / "s.json",
        {"": _entry(99.0), "busy": _entry(80.0), "fresh": _entry(2.0)},
    )
    assert seats.select_seat(path=path, now=NOW, allow_refresh=False) == "fresh"


def test_unknown_usage_is_eligible_but_ranks_behind_a_measured_seat(
    tmp_path: Path,
) -> None:
    """Unknown is not exhaustion and not zero.

    A measured number is better evidence than an absence, so a seat we can see
    has headroom is preferred -- but an unmeasurable seat is still usable when
    it is the only thing left, which is exactly a brand-new account.
    """
    both = _states(
        tmp_path / "both.json", {"": _entry(99.0), "measured": _entry(20.0), "unknown": _entry(None)}
    )
    assert seats.select_seat(path=both, now=NOW, allow_refresh=False) == "measured"

    only_unknown = _states(
        tmp_path / "only.json", {"": _entry(100.0), "unknown": _entry(None)}
    )
    assert seats.select_seat(path=only_unknown, now=NOW, allow_refresh=False) == "unknown"


def test_a_failed_probe_is_not_eligible(tmp_path: Path) -> None:
    """Unknown-but-reporting differs from could-not-reach-it: a login we cannot
    even confirm works must not be selected."""
    path = _states(
        tmp_path / "s.json", {"": _entry(100.0), "broken": _entry(None, ok=False)}
    )
    assert seats.select_seat(path=path, now=NOW, allow_refresh=False) is None


def test_everything_starved_falls_back_to_host(tmp_path: Path) -> None:
    path = _states(tmp_path / "s.json", {"": _entry(100.0), "seat": _entry(100.0)})
    assert seats.select_seat(path=path, now=NOW, allow_refresh=False) is None


@pytest.mark.parametrize(
    "content",
    ['not json', '[]', '{"version": 2, "seats": {}}', '{"version": 1}'],
)
def test_unusable_state_never_raises_and_yields_host(tmp_path: Path, content) -> None:
    """Selection is an optimisation; it must never fail a dispatch."""
    path = tmp_path / "s.json"
    path.write_text(content)
    assert seats.select_seat(path=path, now=NOW, allow_refresh=False) is None


def test_stale_states_trigger_a_refresh_and_fresh_ones_do_not(tmp_path: Path) -> None:
    path = _states(
        tmp_path / "s.json", {"": _entry(100.0)}, updated_at=NOW - seats.STATE_TTL_S - 1
    )
    calls: list[str] = []

    def refresher(*, path, now):
        calls.append("refreshed")
        return {"version": 1, "updated_at": now, "seats": {"": _entry(100.0), "seat": _entry(1.0)}}

    assert seats.select_seat(path=path, now=NOW, refresher=refresher) == "seat"
    assert calls == ["refreshed"]

    _states(tmp_path / "s.json", {"": _entry(100.0), "seat": _entry(1.0)}, updated_at=NOW)
    calls.clear()
    assert seats.select_seat(path=path, now=NOW, refresher=refresher) == "seat"
    assert calls == [], "a fresh cache must not re-probe"


def test_future_timestamp_is_treated_as_stale(tmp_path: Path) -> None:
    """A clock that moved must not pin a stale cache as fresh forever."""
    doc = json.loads(
        _states(tmp_path / "s.json", {"": _entry(1.0)}, updated_at=NOW + 99999).read_text()
    )
    assert seats.states_are_fresh(doc, now=NOW) is False


def test_refresh_records_every_account_including_unreachable_ones(
    tmp_path: Path, monkeypatch
) -> None:
    """Input path: refresh_states -> the bundled reader, once per account."""
    monkeypatch.setattr(
        seats.grok_usage,
        "accounts",
        lambda: [(None, Path("/host/auth.json")), ("seat", Path("/seat/auth.json"))],
    )

    def reader(*, auth_path, timeout_s, account):
        if account == "seat":
            raise RuntimeError("reader exploded")
        return {"ok": True, "used_percent": 12.0}

    path = tmp_path / "s.json"
    document = seats.refresh_states(path=path, now=NOW, reader=reader)
    assert document["seats"][""]["used_percent"] == 12.0
    # a reader that raised must be recorded as unusable, not dropped silently
    assert document["seats"]["seat"]["ok"] is False
    assert json.loads(path.read_text())["seats"]["seat"]["ok"] is False


# --- folder trust -------------------------------------------------------

def test_trust_guard_refuses_paths_that_are_too_broad(tmp_path: Path) -> None:
    """Root, home, and top-level system dirs are never a project.

    /tmp is checked in its LITERAL form on purpose: resolving first would turn
    it into /private/tmp on macOS, which has enough segments to look like a real
    project while being the same system directory.
    """
    for bad in ("/", str(Path.home()), "/tmp", "/usr", "/etc"):
        with pytest.raises(seats.TrustRefused):
            seats._trust_guard(Path(bad))


def test_ensure_project_trusted_appends_once_and_is_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".grok").mkdir(parents=True)
    project = tmp_path / "repos" / "widget"
    project.mkdir(parents=True)

    assert seats.ensure_project_trusted(home, project) is True
    assert seats.ensure_project_trusted(home, project) is False, "must not double-append"

    text = (home / ".grok" / "trusted_folders.toml").read_text()
    assert text.count(f'[folders."{project.resolve()}"]') == 1
    assert "trusted = true" in text
    assert seats.is_project_trusted(home, project) is True


def test_trust_entry_is_not_glued_onto_a_file_without_a_trailing_newline(
    tmp_path: Path,
) -> None:
    """The entry's LEADING newline is what prevents this.

    Without it, a file not ending in a newline gets the new table header glued
    onto its last line, where it parses as something else entirely."""
    home = tmp_path / "home"
    (home / ".grok").mkdir(parents=True)
    trust = home / ".grok" / "trusted_folders.toml"
    trust.write_text('[folders."/Users/x/a"]\ntrusted = true')  # no trailing newline
    project = tmp_path / "b"
    project.mkdir()

    seats.ensure_project_trusted(home, project)
    text = trust.read_text()
    assert 'trusted = true[folders.' not in text
    for line in text.splitlines():
        assert not (line.startswith("[folders.") and not line.endswith("]"))


def test_a_second_project_does_not_disturb_the_first(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".grok").mkdir(parents=True)
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir(); second.mkdir()
    seats.ensure_project_trusted(home, first)
    seats.ensure_project_trusted(home, second)
    assert seats.is_project_trusted(home, first)
    assert seats.is_project_trusted(home, second)


def test_untrusted_home_reports_false_without_raising(tmp_path: Path) -> None:
    """A home with no grok config at all must answer False, not explode."""
    assert seats.is_project_trusted(tmp_path / "nothing", tmp_path) is False
