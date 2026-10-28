#!/usr/bin/env python3
"""A seat is blocked by what is TRUE NOW, not by the last thing we wrote down.

Two independent readers in `_account_quota_blocked` answer "is this seat out of
tokens?" from records rather than from a measurement, and neither could be
cleared by the seat actually recovering:

* the codex snapshot reader (`_effective_account_cooldown`) returned a
  `cooldown_until` from `codex-seat-states.json` without ever consulting the
  `updated_at` sitting beside it in the same file;
* the ledger scan infers "still exhausted" from any past record whose retry
  policy has not come eligible.

Both went wrong at once on 2026-09-06. codex-seatd had been refusing every
300s tick for 31.7h ("codex-version-drift-refusing expected=0.144.5" against an
installed codex-cli 0.153.3), freezing the snapshot on its 2026-09-04 probe:
`worst_used` 100.0 and a 2026-09-07 cooldown on all four seats. A probe taken
that same minute measured cf9f50 at 1% used and 25ca6b at 47%. Every codex seat
read blocked and `_first_unblocked_account("codex")` returned None. The grok
side had the same shape from the other reader: the live probe read rpp usable
at 38% while the ledger scan still returned True from that morning's 402s.

Note the daemon's refusal was CORRECT -- it would not touch seats under a
version it could not vouch for. The defect is that the refusal never reached
the reader: an unknown preserved upstream became a definite "walled" here.

The rule these pin: a fresh measurement beats a stale inference, and only a
DEFINITE fresh measurement does -- an unreadable or stale one is unknown and
leaves the conservative path intact.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402
import goalflight_doctor as DOC  # noqa: E402
import goalflight_ledger as L  # noqa: E402
import grok_seats  # noqa: E402


SEAT = "cf9f50"
FUTURE_RESET = "2099-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Never read or write the operator's real seat states or ledger."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(state))
    monkeypatch.setenv("GOALFLIGHT_CODEX_STATE_DIR", str(state))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", "/dev/null")
    # grok_seats resolves its state path at import, so HOME alone does not
    # contain it: without this, an un-patched probe read the operator's real
    # grok-seat-states.json and the override fired on live seat data.
    monkeypatch.setattr(grok_seats, "load_states", dict)


def _write_snapshot(age_s: float | None, *, updated_at: object = None) -> None:
    """Seed codex-seat-states.json aged `age_s` seconds (or a raw override)."""
    state = Path(__import__("os").environ["GOALFLIGHT_CODEX_STATE_DIR"])
    payload = {
        "version": 1,
        "seats": {SEAT: {"cooldown_until": FUTURE_RESET, "worst_used": 100.0}},
    }
    if age_s is not None:
        stamp = time.gmtime(time.time() - age_s)
        payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", stamp)
    if updated_at is not None:
        payload["updated_at"] = updated_at
    (state / "codex-seat-states.json").write_text(json.dumps(payload))


# --------------------------------------------------------------------------
# the codex snapshot reader
# --------------------------------------------------------------------------


def test_fresh_snapshot_still_supplies_its_cooldown() -> None:
    """The guard must not simply disable cooldowns; a current file still counts."""
    _write_snapshot(age_s=60.0)
    assert D._effective_account_cooldown(SEAT) == FUTURE_RESET


def test_stale_snapshot_supplies_no_cooldown() -> None:
    """31.7h is what the frozen daemon actually produced."""
    _write_snapshot(age_s=31.7 * 3600)
    assert D._effective_account_cooldown(SEAT) is None


def test_snapshot_that_cannot_date_itself_supplies_no_cooldown() -> None:
    """No `updated_at` means it cannot claim to be current -- same as malformed."""
    _write_snapshot(age_s=None)
    assert D._effective_account_cooldown(SEAT) is None
    _write_snapshot(age_s=None, updated_at="not-a-timestamp")
    assert D._effective_account_cooldown(SEAT) is None


def test_the_freshness_boundary_is_the_documented_one() -> None:
    """Pin the constant to behaviour so a silent retune shows up as a failure."""
    ttl = D.CODEX_SEAT_STATE_MAX_AGE_S
    _write_snapshot(age_s=ttl - 60)
    assert D._effective_account_cooldown(SEAT) == FUTURE_RESET
    _write_snapshot(age_s=ttl + 60)
    assert D._effective_account_cooldown(SEAT) is None


def test_stale_codex_seat_state_ticks_once_and_rechecks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_snapshot(age_s=D.CODEX_SEAT_STATE_MAX_AGE_S + 3600)
    attempts: list[None] = []

    def refresh() -> None:
        attempts.append(None)
        payload = {
            "version": 1,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "seats": {SEAT: {"healthy": True, "worst_used": 40.0}},
        }
        state = Path(os.environ["GOALFLIGHT_CODEX_STATE_DIR"])
        (state / "codex-seat-states.json").write_text(json.dumps(payload))

    monkeypatch.setattr(D, "_refresh_codex_seatd_once", refresh)
    assert D._codex_seat_admission_reason(SEAT) is None
    assert len(attempts) == 1


def test_still_stale_codex_seat_state_reports_refresh_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_snapshot(age_s=D.CODEX_SEAT_STATE_MAX_AGE_S + 3600)
    monkeypatch.setattr(D, "CODEX_SEAT_REFRESH_WAIT_S", 0)
    monkeypatch.setattr(D, "_refresh_codex_seatd_once", lambda: None)

    reason = D._codex_seat_admission_reason(SEAT)

    assert reason is not None
    assert reason.startswith("seat state stale since ")
    assert "seat refresh in progress (started None); retry shortly" in reason


def test_seatd_refresh_is_detached_and_never_waits_or_kills(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    monkeypatch.setenv("GOALFLIGHT_CODEX_STATE_DIR", str(state))
    calls: list[dict] = []

    class SlowTick:
        def wait(self, *_args, **_kwargs):
            raise AssertionError("admission must not wait on the child process")

        def kill(self):
            raise AssertionError("credential refresh ticks must never be killed")

    monkeypatch.setattr(
        D.subprocess,
        "Popen",
        lambda *args, **kwargs: calls.append(kwargs) or SlowTick(),
    )

    D._refresh_codex_seatd_once()

    assert len(calls) == 1
    assert calls[0]["start_new_session"] is True
    assert calls[0]["stdin"] == D.subprocess.DEVNULL

    lock_path = state / "codex-active-seat.lock"
    with lock_path.open("a+") as lock_file:
        D.goalflight_compat.flock(lock_file, D.goalflight_compat.LOCK_EX)
        D._refresh_codex_seatd_once()
        D.goalflight_compat.flock(lock_file, D.goalflight_compat.LOCK_UN)
    assert len(calls) == 1


def test_seatd_refresh_stamp_closes_child_startup_gap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    monkeypatch.setenv("GOALFLIGHT_CODEX_STATE_DIR", str(state))
    awake = [100.0]
    monkeypatch.setattr(D, "active_monotonic", lambda: awake[0])
    calls: list[dict] = []
    monkeypatch.setattr(
        D.subprocess,
        "Popen",
        lambda *args, **kwargs: calls.append(kwargs) or object(),
    )

    D._refresh_codex_seatd_once()
    assert len(calls) == 1

    # Model Popen returning before the detached child has acquired seatd's lock.
    lock_path = state / "codex-active-seat.lock"
    with lock_path.open("a+") as seatd_lock:
        D.goalflight_compat.flock(
            seatd_lock,
            D.goalflight_compat.LOCK_EX | D.goalflight_compat.LOCK_NB,
        )
        D.goalflight_compat.flock(seatd_lock, D.goalflight_compat.LOCK_UN)
    D._refresh_codex_seatd_once()
    assert len(calls) == 1

    awake[0] += D.CODEX_SEAT_REFRESH_STARTUP_ALLOWANCE_S + 1.0
    with lock_path.open("a+") as seatd_lock:
        D.goalflight_compat.flock(seatd_lock, D.goalflight_compat.LOCK_EX)
        D._refresh_codex_seatd_once()
        assert len(calls) == 1
        D.goalflight_compat.flock(seatd_lock, D.goalflight_compat.LOCK_UN)

    D._refresh_codex_seatd_once()
    assert len(calls) == 2


def test_stale_grok_state_runs_one_bounded_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = {"updated_at": grok_seats._now() - grok_seats.STATE_TTL_S - 3600, "seats": {}}
    fresh = {
        "updated_at": grok_seats._now(),
        "seats": {
            SEAT: {
                "auth_state": "valid",
                "probe_state": "usable",
                "used_percent": 42.0,
            }
        },
    }
    calls: list[float] = []
    monkeypatch.setattr(grok_seats, "load_states", lambda: stale)
    monkeypatch.setattr(
        grok_seats,
        "refresh_states",
        lambda *, timeout_s: calls.append(timeout_s) or fresh,
    )

    assert D._seat_probe_says_usable(SEAT, "grok") is True
    assert calls == [grok_seats.PROBE_TIMEOUT_S]


def test_dropping_a_stale_cooldown_is_not_fail_open() -> None:
    """A seat walled TODAY stays blocked through the ledger, snapshot or not.

    This is the claim that makes the freshness guard safe to ship: it releases
    only seats whose sole evidence was the frozen file.
    """
    _write_snapshot(age_s=31.7 * 3600)
    assert D._effective_account_cooldown(SEAT) is None, "precondition: no cooldown"
    L.write_record(
        {
            "dispatch_id": "codex-probe-1",
            "agent": "codex",
            "engine": "codex",
            "effective_account": SEAT,
            "state": "quota_exhausted",
            "reason": {
                "limit_kind": "exhausted",
                "limit_state": "quota_exhausted",
                "provider": "openai",
                "reset_at": FUTURE_RESET,
            },
            "updated_at": L.utc_now(),
        }
    )
    assert D._account_quota_blocked(SEAT, engine="codex") is True


# --------------------------------------------------------------------------
# the grok probe override
# --------------------------------------------------------------------------


def _seed_blocking_grok_record(seat: str) -> None:
    L.write_record(
        {
            "dispatch_id": "grok-probe-1",
            "agent": "grok-code",
            "engine": "grok",
            "effective_account": seat,
            "state": "quota_exhausted",
            "reason": {
                "limit_kind": "exhausted",
                "limit_state": "quota_exhausted",
                "provider": "xai",
                "reset_at": FUTURE_RESET,
            },
            "updated_at": L.utc_now(),
        }
    )


def _probe_document(*, age_s: float, **record: object) -> dict:
    return {"updated_at": time.time() - age_s, "seats": {"rpp": dict(record)}}


def test_a_fresh_usable_probe_clears_the_ledger_inference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_blocking_grok_record("rpp")
    assert D._seat_probe_says_usable("rpp", "grok") is None, "no probe yet"
    assert D._account_quota_blocked("rpp", engine="grok") is True, (
        "precondition: the ledger record must block, or this proves nothing"
    )
    monkeypatch.setattr(
        grok_seats,
        "load_states",
        lambda: _probe_document(
            age_s=30.0, auth_state="valid", probe_state="usable", used_percent=38.0
        ),
    )
    assert D._seat_probe_says_usable("rpp", "grok") is True
    assert D._account_quota_blocked("rpp", engine="grok") is False


@pytest.mark.parametrize(
    "label, age_s, record",
    [
        ("stale", 10 * 3600, dict(auth_state="valid", probe_state="usable", used_percent=38.0)),
        ("walled", 30.0, dict(auth_state="valid", probe_state="unusable", used_percent=100.0)),
        ("auth invalid", 30.0, dict(auth_state="invalid", probe_state="usable", used_percent=1.0)),
        ("no measurement", 30.0, dict(auth_state="valid", probe_state="usable")),
    ],
)
def test_only_a_definite_fresh_usable_probe_overrides(
    monkeypatch: pytest.MonkeyPatch, label: str, age_s: float, record: dict
) -> None:
    """Anything short of a measured `usable` leaves the conservative path alone."""
    _seed_blocking_grok_record("rpp")
    monkeypatch.setattr(
        grok_seats, "load_states", lambda: _probe_document(age_s=age_s, **record)
    )
    assert D._seat_probe_says_usable("rpp", "grok") is not True, label
    assert D._account_quota_blocked("rpp", engine="grok") is True, label


def test_an_unreadable_probe_is_unknown_not_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> dict:
        raise OSError("permission denied")

    monkeypatch.setattr(grok_seats, "load_states", _boom)
    assert D._seat_probe_says_usable("rpp", "grok") is None


def test_an_engine_with_no_probe_at_all_stays_unknown() -> None:
    assert D._seat_probe_says_usable(SEAT, None) is None
    assert D._seat_probe_says_usable(SEAT, "moonshot") is None


# --------------------------------------------------------------------------
# the codex arm: the seat daemon's own snapshot is the fresh measurement
#
# This arm only became possible once the daemon was ticking again. While it
# was refusing (31.7h of version drift), the snapshot was stale and every one
# of these returns None -- which is the point: the override is available
# exactly when there is a current measurement behind it, and silently absent
# when there is not.
# --------------------------------------------------------------------------


def _write_probe(**record: object) -> None:
    """Seed a FRESH snapshot carrying one seat record."""
    import os

    state = Path(os.environ["GOALFLIGHT_CODEX_STATE_DIR"])
    payload = {
        "version": 1,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "seats": {SEAT: dict(record)},
    }
    (state / "codex-seat-states.json").write_text(json.dumps(payload))


def test_a_fresh_daemon_measurement_clears_the_ledger_inference() -> None:
    """The seat the daemon just measured at 19% must not read as exhausted."""
    L.write_record(
        {
            "dispatch_id": "codex-old-1",
            "agent": "codex",
            "engine": "codex",
            "effective_account": SEAT,
            "state": "quota_exhausted",
            "reason": {
                "limit_kind": "exhausted",
                "limit_state": "quota_exhausted",
                "provider": "openai",
                "reset_at": FUTURE_RESET,
            },
            "updated_at": L.utc_now(),
        }
    )
    assert D._seat_probe_says_usable(SEAT, "codex") is None, "no snapshot yet"
    assert D._account_quota_blocked(SEAT, engine="codex") is True, (
        "precondition: the ledger record must block, or this proves nothing"
    )
    _write_probe(healthy=True, worst_used=19.0, cooldown_until=None)
    assert D._seat_probe_says_usable(SEAT, "codex") is True
    assert D._account_quota_blocked(SEAT, engine="codex") is False


@pytest.mark.parametrize(
    "label, record",
    [
        ("at the exhaustion mark", dict(healthy=True, worst_used=100.0)),
        ("daemon says unhealthy", dict(healthy=False, worst_used=1.0)),
        ("daemon wrote a live cooldown", dict(healthy=True, worst_used=1.0,
                                              cooldown_until=FUTURE_RESET)),
    ],
)
def test_a_measured_exhausted_seat_is_not_overridden(label: str, record: dict) -> None:
    _write_probe(**record)
    assert D._seat_probe_says_usable(SEAT, "codex") is False, label


@pytest.mark.parametrize(
    "label, record",
    [
        ("no headroom figure", dict(healthy=True)),
        ("headroom is not a number", dict(healthy=True, worst_used="lots")),
        ("cooldown cannot be parsed", dict(healthy=True, worst_used=1.0,
                                           cooldown_until="soon")),
    ],
)
def test_an_unreadable_measurement_is_unknown_not_usable(label: str, record: dict) -> None:
    """Unknown must stay unknown; only a definite True overrides."""
    _write_probe(**record)
    assert D._seat_probe_says_usable(SEAT, "codex") is None, label


def test_a_stale_snapshot_cannot_vouch_for_a_codex_seat() -> None:
    """The 31.7h-frozen-daemon case: the override must simply not be available."""
    _write_snapshot(age_s=31.7 * 3600)
    assert D._seat_probe_says_usable(SEAT, "codex") is None


def test_a_seat_absent_from_the_snapshot_is_unknown() -> None:
    _write_probe(healthy=True, worst_used=1.0)
    assert D._seat_probe_says_usable("nosuchseat", "codex") is None


# --------------------------------------------------------------------------
# the seat-scoped session map
# --------------------------------------------------------------------------


def test_only_engines_with_a_verified_seat_scoped_layout_are_listed() -> None:
    """Membership here drives a real file migration; a guess would move nothing.

    cursor was listed on the strength of its name: its account home holds only
    config JSON, its state lives in ~/.cursor/{acp-sessions,projects} in a
    different shape, and a cursor dispatch records no engine session handle at
    all -- verified 2026-09-06 against its status.json, which carries only
    controller_session_id. codex is absent because --codex-resume-home reads
    the rollout from the original home whatever seat is billed.
    """
    assert D.SEAT_SCOPED_SESSION_ENGINES == {"grok": ".grok"}


# --------------------------------------------------------------------------
# saying it out loud: the reader ignoring a stale snapshot stops the WEDGE,
# but a fleet running on ledger inference alone still needs to be visible.
# --------------------------------------------------------------------------


def test_doctor_is_quiet_while_the_daemon_is_writing() -> None:
    _write_probe(healthy=True, worst_used=19.0)
    got = DOC.check_seat_state_freshness()
    assert got["fresh"] is True
    assert "warning" not in got, got


def test_doctor_warns_when_seat_states_exist_but_probes_are_stale() -> None:
    state = Path(__import__("os").environ["GOALFLIGHT_CODEX_STATE_DIR"])
    payload = {
        "version": 1,
        "updated_at": time.time(),
        "seats": {
            SEAT: {
                "probed_at": time.time() - 31 * 60,
                "healthy": True,
                "worst_used": 19.0,
            }
        },
    }
    (state / "codex-seat-states.json").write_text(json.dumps(payload))

    got = DOC.check_seat_state_freshness()

    assert got["fresh"] is False
    warning = got["warning"]
    assert "probed_at" in warning
    assert "daemon" in warning
    assert "crashed" in warning and "publish" in warning
    assert str(state / "codex-accountd.log") in warning


def test_doctor_names_a_stopped_daemon_and_its_known_cause() -> None:
    """The 31.7h case. A warning nobody can act on is not much better."""
    _write_snapshot(age_s=31.7 * 3600)
    got = DOC.check_seat_state_freshness()
    assert got["fresh"] is False
    warning = got["warning"]
    assert "31.7h" in warning, warning
    assert "unmanaged" in warning, warning
    assert "version" in warning and "pin" in warning, "must point at the known cause"


def test_doctor_distinguishes_an_absent_snapshot_from_a_stale_one() -> None:
    got = DOC.check_seat_state_freshness()
    assert got["fresh"] is False
    assert "no seat-health snapshot" in got["warning"]
    assert "age_s" not in got, "there is no age to report when nothing was written"
