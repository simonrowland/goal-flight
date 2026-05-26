#!/usr/bin/env python3
"""Tests for fleet dispatch row classification (Track A goals 9b/9d)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_fleet_mirror as mirror
import goalflight_fleet_status as status


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def _mirror(state: str, *, seq: int = 1) -> mirror.MirrorReadResult:
    return mirror.MirrorReadResult(
        ok=True,
        payload={
            "schema": mirror.STATUS_MIRROR_SCHEMA,
            "seq": seq,
            "dispatch_id": "acp-test",
            "state": state,
        },
        last_seq=seq,
    )


def test_ssh_down_active_lease_is_unknown_partition() -> None:
    row = status.classify_dispatch_row(
        ssh_reachable=False,
        lease_active=True,
        pid_hint="unknown",
    )
    assert_true("state", row.state == "unknown")
    assert_true("reason", row.quarantine_reason == status.QUARANTINE_SSH_PARTITION)


def test_mirror_stale_running_pid_is_quarantined_not_released() -> None:
    row = status.classify_dispatch_row(
        ssh_reachable=True,
        mirror=_mirror("running"),
        mirror_stale=True,
        lease_active=True,
        pid_hint="alive",
    )
    assert_true("state", row.state == "quarantined")
    assert_true("reason", row.quarantine_reason == status.QUARANTINE_MIRROR_STALE)
    assert_true("no release", status.may_release_locks(row) is False)


def test_running_alive_refresh_path() -> None:
    row = status.classify_dispatch_row(
        ssh_reachable=True,
        mirror=_mirror("running"),
        lease_active=True,
        pid_hint="alive",
    )
    assert_true("state", row.state == "running")
    assert_true("no quarantine", row.quarantine_reason is None)


def test_running_missing_lease_incident() -> None:
    row = status.classify_dispatch_row(
        ssh_reachable=True,
        mirror=_mirror("running"),
        lease_active=False,
        pid_hint="alive",
    )
    assert_true("state", row.state == "quarantined")
    assert_true("reason", row.quarantine_reason == status.QUARANTINE_LEASE_MISSING)


def test_mirror_stale_never_releases() -> None:
    row = status.classify_dispatch_row(
        ssh_reachable=True,
        mirror=_mirror("running"),
        mirror_stale=True,
        lease_active=True,
        pid_hint="alive",
    )
    assert_true("quarantined", row.state == "quarantined")
    assert_true("guard", status.may_release_locks(row) is False)


def test_mirror_stale_unknown_never_releases() -> None:
    """Stale mirror without running+alive hint stays unknown; still must not release."""
    row = status.classify_dispatch_row(
        ssh_reachable=True,
        mirror=_mirror("complete"),
        mirror_stale=True,
        lease_active=True,
        pid_hint="dead",
    )
    assert_true("state", row.state == "unknown")
    assert_true("reason", row.quarantine_reason == status.QUARANTINE_MIRROR_STALE)
    assert_true("guard", status.may_release_locks(row) is False)


def test_terminal_dead_pid_may_release() -> None:
    """Terminal remote truth with dead PID hint prefigures 10b reconcile release."""
    row = status.classify_dispatch_row(
        ssh_reachable=True,
        mirror=_mirror("complete"),
        lease_active=True,
        pid_hint="dead",
    )
    assert_true("state", row.state == "terminal")
    assert_true("may release", status.may_release_locks(row) is True)


def test_orphan_lease_dead_pid_may_release() -> None:
    row = status.classify_dispatch_row(
        ssh_reachable=True,
        mirror=mirror.MirrorReadResult(ok=False, error=mirror.ERROR_MISSING_FILE),
        lease_active=True,
        pid_hint="dead",
    )
    assert_true("reconciling", row.state == "reconciling")
    assert_true("may release", status.may_release_locks(row) is True)


def main() -> None:
    test_ssh_down_active_lease_is_unknown_partition()
    test_mirror_stale_running_pid_is_quarantined_not_released()
    test_running_alive_refresh_path()
    test_running_missing_lease_incident()
    test_mirror_stale_never_releases()
    test_mirror_stale_unknown_never_releases()
    test_terminal_dead_pid_may_release()
    test_orphan_lease_dead_pid_may_release()
    print("OK: fleet status tests pass")


if __name__ == "__main__":
    main()
