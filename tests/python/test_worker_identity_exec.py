"""Pure regression coverage for worker identity across exec(2)."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch  # noqa: E402
import goalflight_ledger  # noqa: E402
import goalflight_session_status  # noqa: E402
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
    expected = _identity(comm="/opt/homebrew/Frameworks/Python.framework/Versions/3.12/Python")
    current = _identity(comm="node")

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


def test_same_second_unrelated_comm_is_still_pid_reuse_without_fine_token() -> None:
    expected = _identity(start_token=None, comm="grok")
    current = _identity(start_token=None, comm="node")

    assert goalflight_ledger.compare_process_identities(PID, expected, current) == (
        False,
        "pid_reused_lstart_comm",
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
            (True, "identity_inconclusive_missing_expected_lstart_comm"),
        ),
        (
            _identity(start_token=None),
            {"pid": PID, "lstart": None, "comm": None},
            (True, "identity_inconclusive_missing_current_lstart_comm"),
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
            (True, "live"),
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

    legacy_expected = _identity(start_token=None, comm="grok")
    legacy_reused = _identity(start_token=None, comm="node")
    monkeypatch.setattr(goalflight_ledger, "process_identity", lambda _pid: legacy_reused)
    assert goalflight_ledger.identity_matches(
        {"worker_pid": PID, "worker_identity": legacy_expected}
    ) == (False, "pid_reused_comm")


def test_fine_start_token_survives_snapshot_and_watcher_projection(monkeypatch) -> None:
    goalflight_compat = goalflight_ledger.goalflight_compat
    monkeypatch.setattr(goalflight_compat, "is_windows", lambda: False)
    monkeypatch.setattr(goalflight_compat, "pid_alive", lambda _pid: True)
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
