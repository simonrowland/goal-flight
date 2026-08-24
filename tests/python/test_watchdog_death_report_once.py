"""A missing watchdog is announced once per generation, not on every arm.

A missing watchdog lock is a STANDING condition. Announcing it costs the
announcing doorbell its life, so a level-triggered announcement means every
replacement doorbell fires on arrival and the pool churns without ever carrying
mail. Measured across the fleet before this fix: no controller held a listener
longer than three minutes, one was churning twelve at once, and a doorbell armed
against an absent watchdog died after fifteen seconds having delivered nothing.

Coverage still reports the gap on every status read, which is where a standing
condition belongs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import goalflight_wake as wake  # noqa: E402


def test_first_claim_wins_and_later_arms_stay_silent(tmp_path: Path) -> None:
    kw = {"controller_label": "ctl", "lease_nonce": "nonce-1"}
    assert wake.claim_watchdog_death_report(tmp_path, **kw) is True
    # Every later arm in the same generation must decline, so it stays armed
    # and keeps delivering mail instead of re-announcing a known absence.
    for _ in range(5):
        assert wake.claim_watchdog_death_report(tmp_path, **kw) is False


def test_a_new_generation_may_announce_again(tmp_path: Path) -> None:
    """The claim is per generation: a fresh lease re-arms the announcement."""
    assert wake.claim_watchdog_death_report(
        tmp_path, controller_label="ctl", lease_nonce="nonce-1"
    ) is True
    assert wake.claim_watchdog_death_report(
        tmp_path, controller_label="ctl", lease_nonce="nonce-2"
    ) is True


def test_separate_controllers_do_not_share_a_claim(tmp_path: Path) -> None:
    assert wake.claim_watchdog_death_report(
        tmp_path, controller_label="alpha", lease_nonce="n"
    ) is True
    assert wake.claim_watchdog_death_report(
        tmp_path, controller_label="beta", lease_nonce="n"
    ) is True


@pytest.mark.parametrize(
    "label,nonce",
    [("", "n"), ("ctl", ""), ("   ", "n"), ("ctl", "   ")],
)
def test_missing_identity_is_refused(tmp_path: Path, label: str, nonce: str) -> None:
    """Never claim on a blank identity — that would silence a real generation."""
    with pytest.raises(ValueError):
        wake.claim_watchdog_death_report(
            tmp_path, controller_label=label, lease_nonce=nonce
        )
