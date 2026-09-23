"""Startup renewal must not claim, expire, or mutate refused generations."""

from contextlib import ExitStack
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import goalflight_journal as journal
import goalflight_wake as wake
import goalflight_wake_supervise as supervise


def snapshot(authority):
    with sqlite3.connect(authority.path) as connection:
        return tuple(connection.iterdump())


@pytest.fixture
def startup(tmp_path, monkeypatch):
    monkeypatch.delenv("GOALFLIGHT_DISPATCH_ID", raising=False)
    monkeypatch.delenv("GOALFLIGHT_TEST_MODE", raising=False)
    monkeypatch.setattr(supervise, "_stdout_is_regular_file", lambda _stream: None)
    project = tmp_path / "project"
    project.mkdir()
    authority = journal.open_or_create_journal(project)
    claim = authority.claim_or_renew_lease(
        "controller", principal={"principal_id": "controller"}, horizon_s=30,
    )
    assert claim.committed and claim.value is not None
    lease = claim.value
    assert authority.arm_listener(
        lease.label, nonce=lease.nonce, pid=123, start_token="test", parent_pid=124,
    ).committed
    args = SimpleNamespace(
        project_root=str(project), controller_label=lease.label,
        lease_nonce=lease.nonce, heartbeat_secs=3600, coverage_secs=3600,
        debug=False, chatty=False,
    )
    monkeypatch.setattr(
        supervise, "run_supervisor", lambda **_kwargs: pytest.fail("must not arm"),
    )
    with ExitStack() as holders:
        holders.enter_context(wake.register_lease_holder(
            project, controller_label=lease.label, lease_nonce=lease.nonce,
        ))
        yield authority, lease, args, holders


def test_holder_dies_between_validation_and_renewal(startup, monkeypatch, capsys):
    authority, lease, args, holders = startup
    before = snapshot(authority)
    resolve = supervise.resolve_startup_lease_nonce

    def validate_then_die(**kwargs):
        result = resolve(**kwargs)
        assert result == (lease.nonce, None, None)
        holders.close()
        return result

    monkeypatch.setattr(supervise, "resolve_startup_lease_nonce", validate_then_die)
    assert supervise.cmd_supervise(args) == supervise.SUPERVISE_STOP_EXIT
    assert "did-not-arm" in capsys.readouterr().err
    assert snapshot(authority) == before


@pytest.mark.parametrize("mismatch", ["nonce", "generation", "ended"])
def test_exact_renewal_refuses_without_state_change(startup, mismatch):
    authority, lease, _args, _holders = startup
    if mismatch == "ended":
        assert authority.release_lease(lease.label, nonce=lease.nonce).committed
    before = snapshot(authority)
    result = authority.renew_active_lease(
        lease.label,
        nonce="wrong" if mismatch == "nonce" else lease.nonce,
        generation=lease.generation + (mismatch == "generation"),
    )
    assert result.disposition == journal.WriteDisposition.CAS_LOST
    assert snapshot(authority) == before


def test_exact_renewal_changes_only_deadline(startup):
    authority, lease, _args, _holders = startup
    before = snapshot(authority)
    result = authority.renew_active_lease(
        lease.label, nonce=lease.nonce, generation=lease.generation,
    )
    assert result.committed and result.value is not None
    assert result.value.renew_deadline_at > lease.renew_deadline_at
    after = snapshot(authority)
    assert tuple(line.replace(result.value.renew_deadline_at, lease.renew_deadline_at)
                 for line in after) == before


def test_supervise_refuses_lost_generation_at_write(startup, monkeypatch, capsys):
    authority, _lease, args, _holders = startup
    before = snapshot(authority)
    renew = journal.Journal.renew_active_lease

    def lose_generation(self, label, **kwargs):
        kwargs["generation"] += 1
        return renew(self, label, **kwargs)

    monkeypatch.setattr(journal.Journal, "renew_active_lease", lose_generation)
    assert supervise.cmd_supervise(args) == supervise.SUPERVISE_STOP_EXIT
    assert "did-not-arm" in capsys.readouterr().err
    assert snapshot(authority) == before


@pytest.mark.parametrize("refusal", ["heartbeat", "coverage", "duplicate", "unknown"])
def test_refused_start_does_not_renew(startup, monkeypatch, refusal):
    authority, _lease, args, _holders = startup
    callback = None
    if refusal == "heartbeat":
        args.heartbeat_secs = 0.01
    elif refusal == "coverage":
        args.coverage_secs = -1
    else:
        callback = lambda *_args: None
        monkeypatch.setattr(wake, "_process_listing", lambda: [])
        monkeypatch.setattr(
            wake, "_supervisor_generation_state_from_listing",
            lambda *_args, **_kwargs: (
                wake.SUPERVISOR_RUNNING if refusal == "duplicate"
                else wake.SUPERVISOR_UNKNOWN
            ),
        )
    before = snapshot(authority)
    assert supervise.cmd_supervise(args, on_startup_probe=callback) == supervise.SUPERVISE_START_EXIT
    assert snapshot(authority) == before
