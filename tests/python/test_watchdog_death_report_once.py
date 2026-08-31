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

import errno
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from machine_isolation import isolated_machine_env

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import goalflight_wake as wake  # noqa: E402
import goalflight_journal as journal  # noqa: E402
import goalflight_messages as messages  # noqa: E402


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


def test_cmd_listen_rings_when_watchdog_claim_creation_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for key, value in isolated_machine_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GOALFLIGHT_TEST_MODE", "1")
    monkeypatch.setenv("GOALFLIGHT_WAKE_ENTRY_POLL_S", "0")
    monkeypatch.setattr(wake, "_process_listing", lambda **_kwargs: [])
    project = tmp_path / "project"
    project.mkdir()
    authority = journal.open_or_create_journal(project)
    claimed = authority.claim_or_renew_lease(
        "ctl", principal={"principal_id": "unknown-watchdog-claim"}
    )
    assert claimed.committed and claimed.value is not None
    lease = claimed.value

    real_open = wake.os.open

    def deny_claim(path: object, *args: object, **kwargs: object) -> int:
        if Path(path).name.startswith("watchdog-death-report-v1."):
            raise PermissionError(errno.EACCES, "claim creation denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(wake.os, "open", deny_claim)
    assert messages._watchdog_death_claim_state(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    ) == "unknown"
    monkeypatch.setattr(
        messages,
        "_wake_recovery_action",
        lambda *_args, **_kwargs: {"kind": "arm-component"},
    )
    monkeypatch.setattr(
        wake,
        "coverage_status",
        lambda *_args, **_kwargs: {
            "wake_mode": "persistent",
            "watchdog": {"state": "missing"},
            "live_waiters": 1,
            "target_waiters": 4,
            "missing_components": ["watchdog"],
        },
    )
    args = SimpleNamespace(
        project_root=str(project),
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        poll_secs=0.01,
        listener_slots=1,
        timeout_s=1,
        json=True,
        report_pending=False,
        watch_follow=False,
    )

    with wake.register_lease_holder(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    ):
        code = messages.cmd_listen(args)

    records = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    dead = next(
        record
        for record in records
        if record.get("payload", {}).get("type") == "watchdog-dead"
    )
    assert code == 0
    assert dead["payload"]["claim_state"] == "unknown"


def test_cmd_listen_rings_when_waiter_probe_is_unavailable_and_watchdog_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An indeterminate sibling must not silence a missing watchdog lock."""
    for key, value in isolated_machine_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GOALFLIGHT_TEST_MODE", "1")
    monkeypatch.setenv("GOALFLIGHT_WAKE_ENTRY_POLL_S", "0")
    monkeypatch.setenv("GOALFLIGHT_TEST_LISTENER_START_TOKEN", "test-listener-token")
    monkeypatch.setattr(wake, "_process_listing", lambda **_kwargs: [])
    project = tmp_path / "project"
    project.mkdir()
    authority = journal.open_or_create_journal(project)
    claimed = authority.claim_or_renew_lease(
        "ctl", principal={"principal_id": "unavailable-waiters-missing-watchdog"}
    )
    assert claimed.committed and claimed.value is not None
    lease = claimed.value
    wake.activate_monitor_state(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        heartbeat_s=120,
        dead_after_s=360,
    )
    monkeypatch.setattr(
        messages,
        "_wake_recovery_action",
        lambda *_args, **_kwargs: {"kind": "arm-component"},
    )
    monkeypatch.setattr(
        wake,
        "coverage_status",
        lambda *_args, **_kwargs: {
            "wake_mode": "persistent",
            "reason": "waiter-probe-unavailable",
            "watchdog": {"required": True, "state": "missing", "observed": 0},
            "live_waiters": None,
            "target_waiters": 4,
            "missing_components": ["watchdog"],
        },
    )
    args = SimpleNamespace(
        project_root=str(project),
        controller_label=lease.label,
        lease_nonce=lease.nonce,
        poll_secs=0.01,
        listener_slots=1,
        timeout_s=1,
        json=True,
        report_pending=False,
        watch_follow=False,
    )
    with wake.register_lease_holder(
        project,
        controller_label=lease.label,
        lease_nonce=lease.nonce,
    ):
        code = messages.cmd_listen(args)

    records = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    dead = next(
        record
        for record in records
        if record.get("payload", {}).get("type") == "watchdog-dead"
    )
    assert code == 0
    assert dead["payload"]["type"] == "watchdog-dead"


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
