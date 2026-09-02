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


def _entry(used):
    if used is None:
        probe_state = "unknown"
    elif float(used) >= seats.EXHAUSTED_AT_PERCENT:
        probe_state = "unusable"
    else:
        probe_state = "usable"
    return {
        "ok": probe_state == "usable",
        "probe_state": probe_state,
        "auth_state": "valid",
        "used_percent": used,
        "error": None,
    }


@pytest.mark.parametrize(
    ("used", "eligible"),
    [
        (0.0, True),
        (50.0, True),
        (seats.EXHAUSTED_AT_PERCENT - 0.1, True),
        (seats.EXHAUSTED_AT_PERCENT, False),
        (99.9, False),
        (100.0, False),
    ],
)
def test_flip_threshold_starves_at_the_configured_percent(
    tmp_path: Path, used, eligible
) -> None:
    """A seat is starved at EXHAUSTED_AT_PERCENT spent, not at 100.

    Waiting for 100 means flipping only after dispatches have already started
    failing; the flip has to happen while there is still headroom to flip with.
    The cases are expressed relative to the constant rather than pinned to a
    literal, so tuning the reserve moves the boundary and does not require
    editing the assertion -- an assertion edited to match a changed value is how
    a real regression gets normalised.
    """
    path = _states(tmp_path / "s.json", {"": _entry(100.0), "seat": _entry(used)})
    if eligible:
        assert seats.select_seat(path=path, now=NOW, allow_refresh=False) == "seat"
    else:
        with pytest.raises(seats.NoUsableSeat):
            seats.select_seat(path=path, now=NOW, allow_refresh=False)


@pytest.mark.parametrize(
    "used_percent",
    [float("nan"), float("inf"), float("-inf"), -0.1, 100.1],
)
def test_non_percentage_numeric_values_are_never_usable(
    tmp_path: Path, used_percent: float
) -> None:
    entry = {
        "ok": True,
        "probe_state": "usable",
        "auth_state": "valid",
        "used_percent": used_percent,
        "error": None,
    }
    assert seats._record_probe_state(entry) == "unknown"
    assert seats._rank(entry) is None
    path = _states(tmp_path / "s.json", {"seat": entry})
    with pytest.raises(seats.NoUsableSeat):
        seats.select_seat(path=path, now=NOW, allow_refresh=False)


def test_starved_host_flips_to_a_seat_with_headroom(tmp_path: Path) -> None:
    path = _states(tmp_path / "s.json", {"": _entry(100.0), "seat": _entry(10.0)})
    assert seats.select_seat(path=path, now=NOW, allow_refresh=False) == "seat"


def test_host_is_chosen_by_returning_none(tmp_path: Path) -> None:
    """None means the measured host login is the best usable choice."""
    path = _states(tmp_path / "s.json", {"": _entry(5.0), "seat": _entry(60.0)})
    assert seats.select_seat(path=path, now=NOW, allow_refresh=False) is None


def test_least_used_seat_wins(tmp_path: Path) -> None:
    path = _states(
        tmp_path / "s.json",
        {"": _entry(99.0), "busy": _entry(80.0), "fresh": _entry(2.0)},
    )
    assert seats.select_seat(path=path, now=NOW, allow_refresh=False) == "fresh"


def test_unknown_usage_is_not_selected_even_without_a_measured_seat(
    tmp_path: Path,
) -> None:
    """Unknown is not exhaustion, zero, or permission to dispatch."""
    both = _states(
        tmp_path / "both.json", {"": _entry(99.0), "measured": _entry(20.0), "unknown": _entry(None)}
    )
    assert seats.select_seat(path=both, now=NOW, allow_refresh=False) == "measured"

    only_unknown = _states(
        tmp_path / "only.json", {"": _entry(100.0), "unknown": _entry(None)}
    )
    with pytest.raises(seats.NoUsableSeat):
        seats.select_seat(path=only_unknown, now=NOW, allow_refresh=False)


@pytest.mark.parametrize("status", [401, 403])
def test_an_auth_failure_is_not_eligible(tmp_path: Path, status: int) -> None:
    """An auth rejection establishes that the saved login cannot serve work."""
    path = _states(
        tmp_path / "s.json",
        {
            "": _entry(100.0),
            "broken": {
                "ok": False,
                "probe_state": "unusable",
                "auth_state": "invalid",
                "used_percent": None,
                "error": f"billing endpoint returned HTTP {status}",
            },
        },
    )
    with pytest.raises(seats.NoUsableSeat):
        seats.select_seat(path=path, now=NOW, allow_refresh=False)


def test_everything_starved_reports_no_usable_seat(tmp_path: Path) -> None:
    path = _states(tmp_path / "s.json", {"": _entry(100.0), "seat": _entry(100.0)})
    with pytest.raises(seats.NoUsableSeat):
        seats.select_seat(path=path, now=NOW, allow_refresh=False)


@pytest.mark.parametrize(
    "content",
    ['not json', '[]', '{"version": 2, "seats": {}}', '{"version": 1}'],
)
def test_unusable_state_reports_no_usable_seat(tmp_path: Path, content) -> None:
    path = tmp_path / "s.json"
    path.write_text(content)
    with pytest.raises(seats.NoUsableSeat):
        seats.select_seat(path=path, now=NOW, allow_refresh=False)


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


def test_cached_auth_failure_is_not_fresh_and_select_reprobes(tmp_path: Path) -> None:
    """A cached HTTP 401 is not a stable measurement.

    Serving it for the rest of the TTL is how a re-authenticated host kept
    rendering as needs-login while a live probe succeeded immediately.
    """
    path = _states(
        tmp_path / "s.json",
        {
            "": {
                "ok": False,
                "probe_state": "unusable",
                "auth_state": "invalid",
                "used_percent": None,
                "error": "billing endpoint returned HTTP 401",
            }
        },
        updated_at=NOW,
    )
    document = seats.load_states(path)
    assert seats.states_are_fresh(document, now=NOW) is False

    calls: list[str] = []

    def refresher(*, path, now):
        del path
        calls.append("refreshed")
        return {
            "version": 1,
            "updated_at": now,
            "seats": {"": _entry(12.0)},
        }

    assert seats.select_seat(path=path, now=NOW, refresher=refresher) is None
    assert calls == ["refreshed"]


def test_cached_transient_failure_is_reprobed_but_not_selected(
    tmp_path: Path,
) -> None:
    """A pre-fix cache must not keep a transient failure benched for its TTL."""
    path = _states(
        tmp_path / "s.json",
        {
            "seat": {
                "ok": False,
                "probe_state": "unknown",
                "auth_state": "valid",
                "used_percent": None,
                "error": "billing endpoint returned HTTP 503",
            }
        },
        updated_at=NOW,
    )
    calls: list[str] = []

    def refresher(*, path, now):
        calls.append("refreshed")
        return {
            "version": 1,
            "updated_at": now,
            "seats": {"seat": _entry(None)},
        }

    with pytest.raises(seats.NoUsableSeat):
        seats.select_seat(path=path, now=NOW, refresher=refresher)
    assert calls == ["refreshed"]


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
    # A reader failure says headroom is unknown, not that the saved login died.
    assert document["seats"]["seat"]["ok"] is False
    assert document["seats"]["seat"]["probe_state"] == "unknown"
    assert document["seats"]["seat"]["auth_state"] == "unknown"
    assert document["seats"]["seat"]["used_percent"] is None
    assert json.loads(path.read_text())["seats"]["seat"]["probe_state"] == "unknown"
    assert seats._rank(document["seats"]["seat"]) is None
    assert seats.states_are_fresh(document, now=NOW) is False


def test_refresh_default_path_loads_the_optional_recover_hook(
    tmp_path: Path, monkeypatch
) -> None:
    """Omitting recover= must consult the optional loader, not silently disable it."""
    monkeypatch.setattr(
        seats.grok_usage,
        "accounts",
        lambda: [("seat", Path("/seat/auth.json"))],
    )
    probes = iter(
        [
            {"ok": False, "error": "billing endpoint returned HTTP 401"},
            {"ok": True, "used_percent": 4.0, "error": None},
        ]
    )
    called: list[str] = []

    def reader(*, auth_path, timeout_s, account):
        return next(probes)

    def hook(**kwargs):
        called.append("recovered")
        return kwargs["reader"](
            auth_path=kwargs["auth_path"],
            timeout_s=kwargs["timeout_s"],
            account=kwargs["label"],
        )

    monkeypatch.setattr(seats, "_optional_recover", lambda: hook)
    path = tmp_path / "s.json"
    document = seats.refresh_states(path=path, now=NOW, reader=reader)
    assert called == ["recovered"]
    assert document["seats"]["seat"]["ok"] is True
    assert document["seats"]["seat"]["probe_state"] == "usable"
    assert document["seats"]["seat"]["auth_state"] == "valid"


def test_refresh_recovers_a_401_via_optional_hook_before_recording_dead(
    tmp_path: Path, monkeypatch
) -> None:
    """A lapsed token must be offered to the rotator, then re-recorded healthy.

    This is the b-162 wire: without it the TTL refresh benches a live seat
    until a human notices. The hook is injected so this test does not depend
    on the untracked rotator being present.
    """
    monkeypatch.setattr(
        seats.grok_usage,
        "accounts",
        lambda: [("seat", Path("/seat/auth.json"))],
    )
    probes = iter(
        [
            {"ok": False, "error": "billing endpoint returned HTTP 401"},
            {"ok": True, "used_percent": 12.0, "error": None},
        ]
    )
    recover_calls: list[dict] = []

    def reader(*, auth_path, timeout_s, account):
        return next(probes)

    def recover(**kwargs):
        recover_calls.append(kwargs)
        return kwargs["reader"](
            auth_path=kwargs["auth_path"],
            timeout_s=kwargs["timeout_s"],
            account=kwargs["label"],
        )

    path = tmp_path / "s.json"
    document = seats.refresh_states(
        path=path, now=NOW, reader=reader, recover=recover
    )
    assert recover_calls, "a 401 must be offered to the optional recover hook"
    assert document["seats"]["seat"]["ok"] is True, document
    assert document["seats"]["seat"]["probe_state"] == "usable"
    assert document["seats"]["seat"]["auth_state"] == "valid"
    assert document["seats"]["seat"]["used_percent"] == 12.0
    assert seats._rank(document["seats"]["seat"]) is not None
    persisted = json.loads(path.read_text())
    assert persisted["seats"]["seat"]["ok"] is True


@pytest.mark.parametrize(
    "failure",
    [
        "billing endpoint returned HTTP 503",
        TimeoutError("billing endpoint timed out"),
        ConnectionResetError("billing connection reset"),
    ],
)
def test_refresh_does_not_offer_a_non_401_failure_to_the_rotator(
    tmp_path: Path, monkeypatch, failure
) -> None:
    """A transient probe failure is unknown, retried, and never selectable."""
    monkeypatch.setattr(
        seats.grok_usage,
        "accounts",
        lambda: [("seat", Path("/seat/auth.json"))],
    )
    recover_calls: list[dict] = []

    def reader(*, auth_path, timeout_s, account):
        if isinstance(failure, BaseException):
            raise failure
        return {"ok": False, "error": failure}

    def recover(**kwargs):
        recover_calls.append(kwargs)
        return {"ok": True, "used_percent": 1.0}

    path = tmp_path / "s.json"
    document = seats.refresh_states(
        path=path, now=NOW, reader=reader, recover=recover
    )
    assert recover_calls == [], "a transient failure must not reach the recover hook"
    entry = document["seats"]["seat"]
    assert entry["ok"] is False, document
    assert entry["probe_state"] == "unknown"
    assert entry["auth_state"] == "unknown"
    assert entry["error"]
    assert seats._rank(entry) is None
    assert seats.states_are_fresh(document, now=NOW) is False

    refresh_calls: list[str] = []

    def measured_refresh(*, path, now):
        refresh_calls.append("refreshed")
        return {
            "version": 1,
            "updated_at": now,
            "seats": {"seat": _entry(1.0)},
        }

    assert seats.select_seat(path=path, now=NOW, refresher=measured_refresh) == "seat"
    assert refresh_calls == ["refreshed"]


def test_http_402_is_unusable_when_it_is_the_only_seat(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        seats.grok_usage,
        "accounts",
        lambda: [("seat", Path("/seat/auth.json"))],
    )

    def reader(**_kwargs):
        return {"ok": False, "error": "billing endpoint returned HTTP 402"}

    path = tmp_path / "s.json"
    document = seats.refresh_states(path=path, now=NOW, reader=reader, recover=None)
    entry = document["seats"]["seat"]
    assert entry["probe_state"] == "unusable"
    assert entry["auth_state"] == "valid"
    assert seats._rank(entry) is None
    with pytest.raises(seats.NoUsableSeat):
        seats.select_seat(path=path, now=NOW, allow_refresh=False)


def test_explicit_wall_flag_is_unusable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        seats.grok_usage,
        "accounts",
        lambda: [("seat", Path("/seat/auth.json"))],
    )

    def reader(**_kwargs):
        return {"ok": True, "used_percent": 1.0, "walled": True}

    document = seats.refresh_states(
        path=tmp_path / "s.json", now=NOW, reader=reader, recover=None
    )
    assert document["seats"]["seat"]["probe_state"] == "unusable"
    assert seats._rank(document["seats"]["seat"]) is None


def test_unreadable_auth_is_unknown_not_benched_and_is_retried(
    tmp_path: Path, monkeypatch
) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text('{"issuer::principal": {"key": "token"}}')
    monkeypatch.setattr(seats.grok_usage, "accounts", lambda: [("seat", auth_path)])
    original_read_text = Path.read_text

    def permission_error(path, *args, **kwargs):
        if path == auth_path:
            raise PermissionError("denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", permission_error)
    path = tmp_path / "s.json"
    document = seats.refresh_states(path=path, now=NOW, recover=None)
    entry = document["seats"]["seat"]
    assert entry["probe_state"] == "unknown"
    assert entry["auth_state"] == "unknown"
    assert seats._rank(entry) is None
    assert seats.states_are_fresh(document, now=NOW) is False

    calls: list[str] = []

    def refresher(*, path, now):
        calls.append("retried")
        return {
            "version": 1,
            "updated_at": now,
            "seats": {"seat": _entry(4.0)},
        }

    assert seats.select_seat(path=path, now=NOW, refresher=refresher) == "seat"
    assert calls == ["retried"]


def test_parsed_tokenless_auth_is_invalid_and_unusable(
    tmp_path: Path, monkeypatch
) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text('{"issuer::principal": {"email": "a@b.c"}}')
    monkeypatch.setattr(seats.grok_usage, "accounts", lambda: [("seat", auth_path)])
    path = tmp_path / "s.json"
    document = seats.refresh_states(path=path, now=NOW, recover=None)
    entry = document["seats"]["seat"]
    assert entry["probe_state"] == "unusable"
    assert entry["auth_state"] == "invalid"
    assert seats._rank(entry) is None
    with pytest.raises(seats.NoUsableSeat):
        seats.select_seat(path=path, now=NOW, allow_refresh=False)


def test_rendering_prints_unknown_probe_state(monkeypatch, capsys) -> None:
    document = {
        "version": 1,
        "updated_at": NOW,
        "seats": {"seat": _entry(None)},
    }
    monkeypatch.setattr(seats, "load_states", lambda *args, **kwargs: document)
    monkeypatch.setattr(seats, "refresh_states", lambda *args, **kwargs: document)
    assert seats.main([]) == 0
    output = capsys.readouterr().out
    assert "state=unknown" in output
    assert "selected: (no usable seat)" in output


def test_refresh_records_dead_when_401_survives_recovery(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        seats.grok_usage,
        "accounts",
        lambda: [("seat", Path("/seat/auth.json"))],
    )

    def reader(*, auth_path, timeout_s, account):
        return {"ok": False, "error": "billing endpoint returned HTTP 401"}

    def recover(**kwargs):
        return dict(kwargs["probe"])

    path = tmp_path / "s.json"
    document = seats.refresh_states(
        path=path, now=NOW, reader=reader, recover=recover
    )
    assert document["seats"]["seat"]["ok"] is False
    assert document["seats"]["seat"]["probe_state"] == "unusable"
    assert document["seats"]["seat"]["auth_state"] == "invalid"
    assert seats._rank(document["seats"]["seat"]) is None


@pytest.mark.parametrize("status", [401, 403])
def test_refresh_without_recover_hook_records_auth_failure_as_dead(
    tmp_path: Path, monkeypatch, status: int
) -> None:
    """Without successful recovery, an auth rejection stays unusable."""
    monkeypatch.setattr(
        seats.grok_usage,
        "accounts",
        lambda: [("seat", Path("/seat/auth.json"))],
    )

    def reader(*, auth_path, timeout_s, account):
        return {"ok": False, "error": f"billing endpoint returned HTTP {status}"}

    path = tmp_path / "s.json"
    document = seats.refresh_states(
        path=path, now=NOW, reader=reader, recover=None
    )
    assert document["seats"]["seat"]["ok"] is False
    assert document["seats"]["seat"]["probe_state"] == "unusable"
    assert document["seats"]["seat"]["auth_state"] == "invalid"
    assert json.loads(path.read_text())["seats"]["seat"]["ok"] is False


def test_refresh_swallows_a_recover_hook_that_raises(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        seats.grok_usage,
        "accounts",
        lambda: [("seat", Path("/seat/auth.json"))],
    )

    def reader(*, auth_path, timeout_s, account):
        return {"ok": False, "error": "billing endpoint returned HTTP 401"}

    def recover(**kwargs):
        raise RuntimeError("rotator exploded")

    path = tmp_path / "s.json"
    document = seats.refresh_states(
        path=path, now=NOW, reader=reader, recover=recover
    )
    assert document["seats"]["seat"]["ok"] is False
    assert document["seats"]["seat"]["probe_state"] == "unusable"


def test_missing_rotator_module_is_a_noop_on_refresh(
    tmp_path: Path, monkeypatch
) -> None:
    """A checkout without scripts/ext must not fail a refresh or change the verdict."""
    import builtins

    monkeypatch.setattr(
        seats.grok_usage,
        "accounts",
        lambda: [("seat", Path("/seat/auth.json"))],
    )
    seats._RECOVER_CACHE = seats._UNSET
    original_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == "ext" or (isinstance(name, str) and name.startswith("ext.")):
            raise ModuleNotFoundError("optional rotator absent")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)
    assert seats._optional_recover() is None

    def reader(*, auth_path, timeout_s, account):
        return {"ok": False, "error": "billing endpoint returned HTTP 401"}

    path = tmp_path / "s.json"
    document = seats.refresh_states(path=path, now=NOW, reader=reader)
    assert document["seats"]["seat"]["ok"] is False
    seats._RECOVER_CACHE = seats._UNSET


def test_note_exhausted_if_proven_routes_selection_away_immediately(
    tmp_path: Path,
) -> None:
    """A live 402 must take effect on the next select_seat, not after the TTL."""
    path = _states(
        tmp_path / "s.json",
        {"": _entry(100.0), "seat": _entry(3.0)},
    )
    assert seats.select_seat(path=path, now=NOW, allow_refresh=False) == "seat"

    marked = []

    def marker(seat, **kwargs):
        marked.append((seat, kwargs))
        document = json.loads(path.read_text())
        document["seats"][seat] = {
            "ok": False,
            "probe_state": "unusable",
            "auth_state": "valid",
            "used_percent": seats.EXHAUSTED_AT_PERCENT,
            "error": "quota_exhausted (observed)",
        }
        path.write_text(json.dumps(document))

    record = {
        "engine": "grok",
        "agent": "grok-code",
        "state": "quota_exhausted",
        "effective_account": "seat",
    }
    assert seats.note_exhausted_if_proven(record, path=path, marker=marker) is True
    assert marked and marked[0][0] == "seat"
    with pytest.raises(seats.NoUsableSeat):
        seats.select_seat(path=path, now=NOW, allow_refresh=False)


@pytest.mark.parametrize(
    ("record", "state"),
    [
        (
            {
                "engine": "codex",
                "agent": "codex",
                "state": "quota_exhausted",
                "effective_account": "seat",
            },
            None,
        ),
        (
            {
                "engine": "grok",
                "agent": "grok-code",
                "state": "complete",
                "effective_account": "seat",
            },
            None,
        ),
        (
            {
                "engine": "grok",
                "agent": "grok-code",
                "state": "quota_exhausted",
                "effective_account": "",
            },
            None,
        ),
        (
            {
                "engine": "grok",
                "agent": "grok-code",
                "state": "quota_exhausted",
            },
            None,
        ),
        (
            {
                "engine": "grok",
                "agent": "grok-code",
                "state": "failed",
                "effective_account": "seat",
            },
            "failed",
        ),
    ],
)
def test_note_exhausted_if_proven_ignores_records_that_did_not_prove_it(
    record, state
) -> None:
    marked = []
    assert (
        seats.note_exhausted_if_proven(
            record, state=state, marker=lambda seat, **k: marked.append(seat)
        )
        is False
    )
    assert marked == []


def test_note_exhausted_is_noop_when_rotator_is_absent(monkeypatch) -> None:
    import builtins

    seats._MARK_CACHE = seats._UNSET
    original_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == "ext" or (isinstance(name, str) and name.startswith("ext.")):
            raise ModuleNotFoundError("optional rotator absent")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)
    assert seats._optional_mark_exhausted() is None
    assert seats.note_exhausted("seat") is False
    seats._MARK_CACHE = seats._UNSET


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
