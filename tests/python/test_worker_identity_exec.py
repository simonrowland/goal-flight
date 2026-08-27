"""Pure regression coverage for worker identity across exec(2)."""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_acp_client  # noqa: E402
import goalflight_dispatch  # noqa: E402
import goalflight_fleet_launch_detached  # noqa: E402
import goalflight_ledger  # noqa: E402
import goalflight_session_status  # noqa: E402
import goalflight_status  # noqa: E402
import goalflight_watch  # noqa: E402


PID = 12345
LSTART = "Sun May 31 19:28:48 2026"
START_TOKEN = "darwin:1780262928:123456"


def _identity(**overrides: object) -> dict:
    identity = {
        "pid": PID,
        "start_token": START_TOKEN,
        "lstart": LSTART,
        "comm": "node",
    }
    identity.update(overrides)
    return identity


def test_legitimate_exec_comm_change_is_live() -> None:
    expected = _identity(
        start_token=None,
        comm="/opt/homebrew/Frameworks/Python.framework/Versions/3.12/Python",
    )
    current = _identity(start_token=None, comm="node")

    assert goalflight_ledger.compare_process_identities(PID, expected, current) == (
        True,
        "live",
    )


def test_different_lstart_is_still_pid_reuse() -> None:
    expected = _identity(lstart="expected process start")
    current = _identity(lstart="actual process start")

    assert goalflight_ledger.compare_process_identities(PID, expected, current) == (
        False,
        "pid_reused_lstart",
    )


def test_same_lstart_comm_change_is_live_without_fine_token() -> None:
    expected = _identity(start_token=None, comm="grok")
    current = _identity(start_token=None, comm="node")

    assert goalflight_ledger.compare_process_identities(PID, expected, current) == (
        True,
        "live",
    )


def test_same_second_reuse_is_decisive_with_fine_start_token() -> None:
    expected = _identity(comm="python")
    current = _identity(start_token="darwin:1780262928:123999", comm="node")

    assert goalflight_ledger.compare_process_identities(PID, expected, current) == (
        False,
        "pid_reused_start_token",
    )


def test_cosmetic_comm_variation_stays_live_without_fine_token() -> None:
    expected = _identity(start_token=None, comm="grok")
    current = _identity(start_token=None, comm="(grok-0.2.11-maco)")

    assert goalflight_ledger.compare_process_identities(PID, expected, current) == (
        True,
        "live",
    )


def test_missing_identity_fields_preserve_inconclusive_and_fallback_paths() -> None:
    cases = [
        (
            {"pid": PID, "lstart": None, "comm": None},
            _identity(start_token=None),
            (True, "identity_inconclusive_missing_expected_lstart"),
        ),
        (
            _identity(start_token=None),
            {"pid": PID, "lstart": None, "comm": None},
            (True, "identity_inconclusive_missing_current_lstart"),
        ),
        (
            _identity(start_token=None, comm=None),
            _identity(start_token=None),
            (True, "live"),
        ),
        (
            _identity(start_token=None),
            _identity(start_token=None, comm=None),
            (True, "live"),
        ),
        (
            {"pid": PID, "lstart": None, "comm": "grok"},
            {"pid": PID, "lstart": None, "comm": "(grok-0.2.11-maco)"},
            (True, "identity_inconclusive_missing_expected_current_lstart"),
        ),
    ]

    for expected, current, result in cases:
        assert goalflight_ledger.compare_process_identities(PID, expected, current) == result


def test_watcher_verdict_uses_constructed_identity_comparison(monkeypatch) -> None:
    expected = _identity(comm="python")
    current = _identity(comm="node")
    monkeypatch.setattr(goalflight_ledger, "process_identity", lambda _pid: current)

    assert goalflight_watch.worker_alive(PID, expected) == (True, "live", current)


def test_reap_identity_check_uses_constructed_identity_comparison(monkeypatch) -> None:
    expected = _identity(comm="python")
    current = _identity(comm="node")
    monkeypatch.setattr(goalflight_ledger, "process_identity", lambda _pid: current)

    assert goalflight_ledger.identity_matches(
        {"worker_pid": PID, "worker_identity": expected}
    ) == (True, "live")

    reused = _identity(start_token="darwin:1780262928:123999", comm="node")
    monkeypatch.setattr(goalflight_ledger, "process_identity", lambda _pid: reused)
    assert goalflight_ledger.identity_matches(
        {"worker_pid": PID, "worker_identity": expected}
    ) == (False, "pid_reused_start_token")

    legacy_expected = _identity(start_token=None, comm="python")
    execed_worker = _identity(start_token=None, comm="node")
    monkeypatch.setattr(goalflight_ledger, "process_identity", lambda _pid: execed_worker)
    assert goalflight_ledger.identity_matches(
        {"worker_pid": PID, "worker_identity": legacy_expected}
    ) == (True, "live")


def test_quota_reaper_identity_reader_ignores_exec_comm_change(monkeypatch) -> None:
    expected = _identity(comm="python")
    current = _identity(comm="node")
    monkeypatch.setattr(goalflight_ledger, "process_identity", lambda _pid: current)

    assert goalflight_acp_client._quota_worker_identity_matches(
        {"worker_pid": PID, "worker_identity": expected}, current
    ) == (True, "live")

    reused = _identity(
        lstart="actual process start",
        comm="node",
    )
    monkeypatch.setattr(goalflight_ledger, "process_identity", lambda _pid: reused)
    assert goalflight_acp_client._quota_worker_identity_matches(
        {"worker_pid": PID, "worker_identity": expected}, reused
    ) == (False, "pid_reused_lstart")

    tokenless = _identity(start_token=None, comm="node")
    monkeypatch.setattr(goalflight_ledger, "process_identity", lambda _pid: tokenless)
    assert goalflight_acp_client._quota_worker_identity_matches(
        {"worker_pid": PID, "worker_identity": expected}, tokenless
    ) == (False, "identity_indeterminate")


def test_fleet_identity_readers_ignore_exec_comm_change(monkeypatch) -> None:
    expected = _identity(start_token=None, comm="python")
    current = _identity(start_token=None, comm="node")
    monkeypatch.setattr(
        goalflight_fleet_launch_detached,
        "_process_identity",
        lambda _pid: current,
    )

    assert goalflight_fleet_launch_detached._recorded_worker_live(
        PID, expected
    ) == (True, "live")
    assert goalflight_fleet_launch_detached._receipt_live_identity(
        {"remote_pid": PID, "remote_identity": expected}
    ) == current

    reused = _identity(start_token=None, lstart="actual process start", comm="node")
    monkeypatch.setattr(
        goalflight_fleet_launch_detached,
        "_process_identity",
        lambda _pid: reused,
    )
    assert goalflight_fleet_launch_detached._recorded_worker_live(
        PID, expected
    ) == (False, "pid_reused_lstart")
    assert goalflight_fleet_launch_detached._receipt_live_identity(
        {"remote_pid": PID, "remote_identity": expected}
    ) is None


def test_legacy_fleet_receipt_still_checks_lstart(monkeypatch) -> None:
    current = _identity(start_token=None, comm="node")
    monkeypatch.setattr(
        goalflight_fleet_launch_detached,
        "_process_identity",
        lambda _pid: current,
    )

    assert goalflight_fleet_launch_detached._receipt_live_identity(
        {"remote_pid": PID, "remote_lstart": LSTART}
    ) == current
    assert goalflight_fleet_launch_detached._receipt_live_identity(
        {"remote_pid": PID, "remote_lstart": "actual process start"}
    ) is None


def test_fleet_pid_identity_uses_fine_token(monkeypatch, capsys) -> None:
    expected = _identity(comm="python")
    encoded = base64.b64encode(json.dumps(expected).encode("utf-8")).decode("ascii")
    args = argparse.Namespace(
        pid=PID,
        expected_identity_b64=encoded,
        expected_lstart_b64=None,
    )

    current = _identity(comm="node")
    monkeypatch.setattr(
        goalflight_fleet_launch_detached,
        "_process_identity",
        lambda _pid: current,
    )
    assert goalflight_fleet_launch_detached._pid_identity(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["alive"] is True
    assert payload["identity_reason"] == "live"

    reused = _identity(start_token="darwin:1780262928:123999", comm="node")
    monkeypatch.setattr(
        goalflight_fleet_launch_detached,
        "_process_identity",
        lambda _pid: reused,
    )
    assert goalflight_fleet_launch_detached._pid_identity(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["alive"] is False
    assert payload["identity_reason"] == "pid_reused_start_token"


def test_fleet_partial_identity_retains_legacy_lstart(monkeypatch, capsys) -> None:
    partial = base64.b64encode(json.dumps({"pid": PID}).encode("utf-8")).decode(
        "ascii"
    )
    encoded_lstart = base64.b64encode(LSTART.encode("utf-8")).decode("ascii")
    args = argparse.Namespace(
        pid=PID,
        expected_identity_b64=partial,
        expected_lstart_b64=encoded_lstart,
    )
    current = _identity(
        start_token=None,
        lstart="actual process start",
        comm="node",
    )
    monkeypatch.setattr(
        goalflight_fleet_launch_detached,
        "_process_identity",
        lambda _pid: current,
    )

    assert goalflight_fleet_launch_detached._pid_identity(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["alive"] is False
    assert payload["identity_reason"] == "pid_reused_lstart"


def test_steer_liveness_warning_ignores_exec_comm_change(monkeypatch) -> None:
    expected = _identity(start_token=None, comm="python")
    current = _identity(start_token=None, comm="node")
    record = {
        "dispatch_id": "exec-worker",
        "worker_pid": PID,
        "worker_identity": expected,
    }
    monkeypatch.setattr(goalflight_ledger, "process_identity", lambda _pid: current)

    assert goalflight_dispatch._worker_liveness_warning(record) is None

    reused = _identity(start_token=None, lstart="actual process start", comm="node")
    monkeypatch.setattr(goalflight_ledger, "process_identity", lambda _pid: reused)
    warning = goalflight_dispatch._worker_liveness_warning(record)
    assert warning and "pid_reused_lstart" in warning


def test_lstart_without_comm_remains_a_recorded_identity() -> None:
    identity = {"pid": PID, "lstart": LSTART}

    assert goalflight_dispatch._watch_identity_token(identity) == identity
    assert goalflight_status._has_recorded_worker_identity(
        {"worker_identity": identity}
    )


def test_fine_start_token_survives_snapshot_and_watcher_projection(monkeypatch) -> None:
    goalflight_compat = goalflight_ledger.goalflight_compat
    monkeypatch.setattr(goalflight_compat, "is_windows", lambda: False)
    monkeypatch.setattr(goalflight_compat, "pid_liveness", lambda _pid: True)
    monkeypatch.setattr(
        goalflight_compat,
        "process_start_identity",
        lambda pid: {"pid": pid, "start_token": START_TOKEN},
    )
    monkeypatch.setattr(goalflight_ledger, "_posix_ps_available", lambda: False)

    identity = goalflight_ledger.process_identity(PID)
    assert identity and identity.get("start_token") == START_TOKEN
    assert goalflight_dispatch._watch_identity_token(identity) == {
        "pid": PID,
        "start_token": START_TOKEN,
    }
    assert goalflight_watch._identity_token(identity) == {
        "pid": PID,
        "start_token": START_TOKEN,
    }


def test_controller_snapshot_uses_shared_fine_start_probe(monkeypatch) -> None:
    calls = []

    def process_start_identity(pid: int, *, include_ancestry: bool = False) -> dict:
        calls.append((pid, include_ancestry))
        return {"pid": pid, "start_token": START_TOKEN, "ppid": 12}

    monkeypatch.setattr(
        goalflight_session_status.goalflight_compat,
        "process_start_identity",
        process_start_identity,
    )

    assert goalflight_session_status._controller_process_snapshot(
        PID, include_ancestry=True
    ) == {"pid": PID, "start_token": START_TOKEN, "ppid": 12}
    assert calls == [(PID, True)]


def test_controller_lease_liveness_is_the_lock_probe_never_pid(monkeypatch) -> None:
    # The old contract measured a lease holder by pid + fine start token. That
    # oracle was deliberately DELETED by the kernel-held-lease round: liveness
    # is the held flock, and pid/start-token is audit identity only (kill -9
    # coverage lives in test_wake_layer). This test pins the replacement
    # contract two ways: the deleted oracle must not resurrect, and the
    # evidence wrapper must delegate to the lock probe verbatim.
    assert not hasattr(goalflight_session_status, "_controller_holder_liveness")

    import goalflight_journal
    import goalflight_wake

    probes = []

    def fake_lock_probe(project_root, *, controller_label, lease_nonce, **kwargs):
        probes.append((str(project_root), controller_label, lease_nonce))
        return True

    monkeypatch.setattr(
        goalflight_session_status.goalflight_wake,
        "lease_holder_alive",
        fake_lock_probe,
    )
    lease = goalflight_journal.LeaseIdentity(
        label="probe-label",
        project_root="/tmp/lease-probe-project",
        generation=7,
        nonce="probe-nonce",
        state="ACTIVE",
        claimed_at="2026-08-14T00:00:00+00:00",
        renewed_at="2026-08-14T00:00:00+00:00",
        renew_deadline_at="2026-08-14T01:00:00+00:00",
        principal={"principal_id": "probe"},
    )
    evidence = goalflight_session_status._lease_holder_liveness(lease)
    assert evidence is not None
    assert (evidence.generation, evidence.nonce, evidence.alive) == (7, "probe-nonce", True)
    assert probes == [("/tmp/lease-probe-project", "probe-label", "probe-nonce")]
    assert goalflight_session_status._lease_holder_liveness(None) is None
