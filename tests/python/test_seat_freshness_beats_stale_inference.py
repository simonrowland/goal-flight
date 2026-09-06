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
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402
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


def test_the_override_is_scoped_to_the_engine_that_has_a_probe() -> None:
    """codex has no persisted fresh probe, so it must not claim one."""
    assert D._seat_probe_says_usable(SEAT, "codex") is None
    assert D._seat_probe_says_usable(SEAT, None) is None


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
