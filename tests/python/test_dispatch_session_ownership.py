"""Dispatch ownership is an exact journal-lease claim, never silent inference."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import datetime as dt
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

import goalflight_dispatch as dispatch  # noqa: E402
import goalflight_journal as journal  # noqa: E402
import goalflight_session_status as sessions  # noqa: E402
import goalflight_wake as wake  # noqa: E402


def _state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, journal.Journal]:
    for key, value in {
        "GOALFLIGHT_TASK_STORE_DIR": tmp_path / "task-store",
        "GOALFLIGHT_JOURNAL_DIR": tmp_path / "journal",
        "GOALFLIGHT_MESSAGES_DIR": tmp_path / "messages",
        "GOALFLIGHT_STATE_DIR": tmp_path / "state",
        "GOALFLIGHT_WAKE_LEDGER_DIR": tmp_path / "wake-ledger",
        "GOAL_FLIGHT_PIDFILE_DIR": tmp_path / "pidfiles",
    }.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", "/dev/null")
    root = tmp_path / "project"
    root.mkdir()
    return root, journal.open_or_create_journal(root)


def _args(**overrides):
    values = {
        "controller_label": "owner",
        "controller_beacon_pid": 62001,
        "controller_pid": None,
        "controller_session_id": None,
        "from_queue": False,
        "launch_detached": False,
        "acp_detached_child": False,
        "takeover": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _capture_started_locks(monkeypatch: pytest.MonkeyPatch) -> list:
    holders = []

    def start(project_root, *, label, nonce, pid, start_token):
        del pid, start_token
        holders.append(
            wake.register_lease_holder(
                project_root,
                controller_label=label,
                lease_nonce=nonce,
            )
        )
        return None

    monkeypatch.setattr(sessions, "_start_lock_holder", start)
    return holders


def test_same_principal_claims_twice_without_nonce_and_renews(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _root, authority = _state(monkeypatch, tmp_path)
    principal = {"pid": 62001, "start_token": "incumbent"}
    first = authority.claim_or_renew_lease(
        "owner", principal=principal, horizon_s=5
    )
    second = authority.claim_or_renew_lease(
        "owner", principal=principal, horizon_s=30
    )
    assert first.committed and first.value is not None
    assert second.committed and second.value is not None
    assert second.value.generation == first.value.generation
    assert second.value.nonce == first.value.nonce
    assert second.value.renew_deadline_at > first.value.renew_deadline_at


def test_dead_holder_with_unexpired_deadline_is_replaced_automatically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority = _state(monkeypatch, tmp_path)
    incumbent = authority.claim_or_renew_lease(
        "owner",
        principal={"pid": 62001, "start_token": "incumbent"},
        horizon_s=300,
    )
    assert incumbent.committed and incumbent.value is not None
    identities = {
        62001: None,
        62002: {"pid": 62002, "start_token": "successor"},
    }
    monkeypatch.setattr(sessions, "_controller_process_identity", identities.get)
    monkeypatch.setattr(sessions.goalflight_compat, "pid_alive", lambda pid: pid == 62002)
    holder = wake.register_lease_holder(
        root,
        controller_label="owner",
        lease_nonce=incumbent.value.nonce,
    )
    holder.close()

    claimed = sessions.claim_controller_startup(
        root,
        pid=62002,
        label="owner",
        role="controller",
    )

    assert claimed["claimed"] is True
    assert claimed["session"]["generation"] == incumbent.value.generation + 1
    rows = authority.lease_records(include_ended=True)
    superseded = next(row for row in rows if row["generation"] == incumbent.value.generation)
    assert superseded["state"] == "EXPIRED"
    assert superseded["ended_reason"] == "holder-dead"
    assert superseded["ended_at"] is not None


def test_stale_dead_evidence_cannot_replace_a_new_live_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _root, authority = _state(monkeypatch, tmp_path)
    first = authority.claim_or_renew_lease(
        "owner", principal={"pid": 62001, "start_token": "first"}
    )
    assert first.committed and first.value is not None
    stale_evidence = journal.LeaseLivenessEvidence(
        generation=first.value.generation,
        nonce=first.value.nonce,
        alive=False,
    )
    second = authority.claim_or_renew_lease(
        "owner",
        principal={"pid": 62002, "start_token": "second"},
        takeover=True,
    )
    assert second.committed and second.value is not None

    refused = authority.claim_or_renew_lease(
        "owner",
        principal={"pid": 62003, "start_token": "third"},
        incumbent_liveness=stale_evidence,
    )

    assert refused.cas_lost
    assert authority.active_lease("owner") == second.value


def test_owning_controller_child_renews_with_nonce_and_measured_beacon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority = _state(monkeypatch, tmp_path)
    incumbent = authority.claim_or_renew_lease(
        "owner", principal={"pid": 62001, "start_token": "incumbent"}
    )
    assert incumbent.committed and incumbent.value is not None
    monkeypatch.setattr(
        sessions,
        "_controller_process_identity",
        lambda pid: {"pid": pid, "start_token": "incumbent"},
    )
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_SESSION_ID", incumbent.value.nonce)
    _capture_started_locks(monkeypatch)
    args = _args(controller_session_id=None)
    claimed = dispatch._stamp_controller_session(args, root)
    assert claimed["claimed"] is True
    renewed = authority.active_lease("owner")
    assert renewed is not None
    assert renewed.generation == incumbent.value.generation
    assert renewed.nonce == incumbent.value.nonce
    assert args.controller_session_id == incumbent.value.nonce
    assert args._controller_beacon_pid == 62001

    replay_args = SimpleNamespace(
        **vars(args),
        agent="codex",
        dispatch_id="owner-child",
        cwd=str(root),
        shape="bash",
        priority="normal",
        billing="sub",
        poll_secs=1.0,
        max_idle_secs=60.0,
        prompt_file=None,
        prompt="owned child",
        task_ids=[],
        model=None,
        os_sandbox=None,
        read_only=False,
        fast=False,
        web_research_ok=False,
        web_qa=False,
        ignore_git_warn=True,
        no_orientation=True,
        capacity_wait_s=0,
        account=None,
        interactive=False,
        permission_mode="auto",
        permission_dir=None,
        permission_inline_timeout_s=None,
        permission_user_timeout_s=None,
    )
    replay = dispatch._canonical_replay_argv(
        replay_args,
        [],
        tail=tmp_path / "owner-child.tail",
        status_json=tmp_path / "owner-child.status.json",
    )
    assert replay[replay.index("--controller-session-id") + 1] == incumbent.value.nonce
    assert replay[replay.index("--controller-beacon-pid") + 1] == "62001"


def test_ambient_lease_capability_inherits_owner_without_claiming(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority = _state(monkeypatch, tmp_path)
    incumbent = authority.claim_or_renew_lease(
        "owner", principal={"pid": 62001, "start_token": "incumbent"}
    )
    assert incumbent.committed and incumbent.value is not None
    holder = wake.register_lease_holder(
        root,
        controller_label="owner",
        lease_nonce=incumbent.value.nonce,
    )
    monkeypatch.delenv("GOALFLIGHT_CONTROLLER_SESSION_ID", raising=False)
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_LEASE_NONCE", incumbent.value.nonce)
    monkeypatch.setattr(
        sessions,
        "claim_controller_startup",
        lambda *_args, **_kwargs: pytest.fail("ambient inheritance attempted to claim"),
    )

    args = _args(controller_beacon_pid=None)
    stamped = dispatch._stamp_controller_session(args, root)

    assert stamped["reason"] == "inherited_controller_capability"
    assert args.controller_label == "owner"
    assert args.controller_session_id == incumbent.value.nonce
    assert args._controller_beacon_pid == 62001
    after = authority.active_lease("owner")
    assert after is not None
    assert after.renewed_at == incumbent.value.renewed_at
    assert after.renew_deadline_at == incumbent.value.renew_deadline_at
    holder.close()


def test_mismatched_ambient_lease_capability_stays_unowned_without_claiming(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority = _state(monkeypatch, tmp_path)
    incumbent = authority.claim_or_renew_lease(
        "owner", principal={"pid": 62001, "start_token": "incumbent"}
    )
    assert incumbent.committed and incumbent.value is not None
    holder = wake.register_lease_holder(
        root,
        controller_label="owner",
        lease_nonce=incumbent.value.nonce,
    )
    monkeypatch.delenv("GOALFLIGHT_CONTROLLER_SESSION_ID", raising=False)
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_LEASE_NONCE", "foreign-capability")
    monkeypatch.setattr(
        sessions,
        "claim_controller_startup",
        lambda *_args, **_kwargs: pytest.fail("mismatched capability attempted to claim"),
    )

    args = _args(controller_beacon_pid=None)
    stamped = dispatch._stamp_controller_session(args, root)

    assert stamped["reason"] == "controller_capability_mismatch"
    assert args.controller_label is None
    assert args.controller_session_id is None
    assert args._controller_beacon_pid is None

    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_SESSION_ID", incumbent.value.nonce)
    conflicting = _args(controller_beacon_pid=None)
    conflict = dispatch._stamp_controller_session(conflicting, root)
    assert conflict["reason"] == "conflicting_controller_capabilities"
    assert conflicting.controller_label is None
    assert conflicting.controller_session_id is None
    assert conflicting._controller_beacon_pid is None
    holder.close()


def test_different_live_controller_is_refused_even_with_incumbent_nonce(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority = _state(monkeypatch, tmp_path)
    incumbent = authority.claim_or_renew_lease(
        "owner", principal={"pid": 62001, "start_token": "incumbent"}
    )
    assert incumbent.committed and incumbent.value is not None
    refused = authority.claim_or_renew_lease(
        "owner",
        principal={"pid": 62002, "start_token": "different"},
        nonce=incumbent.value.nonce,
    )
    assert refused.committed is False
    assert "label in use" in str(refused.reason)
    assert authority.active_lease("owner") == incumbent.value


def test_elapsed_legacy_deadline_does_not_make_a_live_holder_takeable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority = _state(monkeypatch, tmp_path)
    incumbent = authority.claim_or_renew_lease(
        "owner", principal={"pid": 62001, "start_token": "incumbent"}
    )
    assert incumbent.committed and incumbent.value is not None
    expired_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).isoformat(
        timespec="seconds"
    )
    expired = authority.write(
        journal.RowOperation.update(
            "controller_leases",
            {"renew_deadline_at": expired_at},
            where={
                "project_root": str(authority.project_root),
                "label": "owner",
                "generation": incumbent.value.generation,
            },
            row_cap=1,
            expected_rows=1,
        )
    )
    assert expired.committed
    holder = wake.register_lease_holder(
        root,
        controller_label="owner",
        lease_nonce=incumbent.value.nonce,
    )
    claimed = authority.claim_or_renew_lease(
        "owner",
        principal={"pid": 62002, "start_token": "successor"},
        incumbent_liveness=sessions._lease_holder_liveness(incumbent.value),
    )
    holder.close()
    assert claimed.cas_lost
    active = authority.active_lease("owner")
    assert active is not None
    assert active.generation == incumbent.value.generation
    assert active.nonce == incumbent.value.nonce
    assert active.principal == incumbent.value.principal
    assert active.renew_deadline_at == expired_at


def test_dispatch_auto_claim_conflict_is_visible_and_never_steals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority = _state(monkeypatch, tmp_path)
    incumbent = authority.claim_or_renew_lease(
        "owner", principal={"pid": 62001, "start_token": "incumbent"}
    )
    assert incumbent.committed and incumbent.value is not None
    identities = {
        62001: {"pid": 62001, "start_token": "incumbent"},
        62002: {"pid": 62002, "start_token": "different"},
    }
    monkeypatch.setattr(sessions, "_controller_process_identity", identities.get)
    holder = wake.register_lease_holder(
        root,
        controller_label="owner",
        lease_nonce=incumbent.value.nonce,
    )
    args = _args(controller_beacon_pid=62002)
    result = dispatch._stamp_controller_session(args, root)
    holder.close()
    assert result["reason"] == "label_in_use"
    assert "label in use" in str(result["message"])
    assert "goalflight_dispatch.py" in str(result["message"])
    assert "--takeover" in str(result["message"])
    assert args.controller_label is None and args.controller_session_id is None
    assert authority.active_lease("owner") == incumbent.value


def test_dispatch_explicit_takeover_supersedes_live_holder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority = _state(monkeypatch, tmp_path)
    incumbent = authority.claim_or_renew_lease(
        "owner", principal={"pid": 62001, "start_token": "incumbent"}
    )
    assert incumbent.committed and incumbent.value is not None
    identities = {
        62001: {"pid": 62001, "start_token": "incumbent"},
        62002: {"pid": 62002, "start_token": "different"},
    }
    monkeypatch.setattr(sessions, "_controller_process_identity", identities.get)
    incumbent_holder = wake.register_lease_holder(
        root,
        controller_label="owner",
        lease_nonce=incumbent.value.nonce,
    )
    _capture_started_locks(monkeypatch)

    args = _args(controller_beacon_pid=62002, takeover=True)
    result = dispatch._stamp_controller_session(args, root)
    incumbent_holder.close()

    assert result["claimed"] is True
    active = authority.active_lease("owner")
    assert active is not None
    assert active.generation == incumbent.value.generation + 1
    assert active.principal["pid"] == 62002
    ended = next(
        row
        for row in authority.lease_records(include_ended=True)
        if row["generation"] == incumbent.value.generation
    )
    assert ended["state"] == "SUPERSEDED"
    assert ended["ended_reason"] == "explicit-takeover"
    assert ended["ended_at"] is not None


def test_dispatch_main_returns_visible_label_in_use_before_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, authority = _state(monkeypatch, tmp_path)
    assert authority.claim_or_renew_lease(
        "owner", principal={"pid": 62001, "start_token": "incumbent"}
    ).committed
    identities = {
        62001: {"pid": 62001, "start_token": "incumbent"},
        62002: {"pid": 62002, "start_token": "different"},
    }
    monkeypatch.setattr(sessions, "_controller_process_identity", identities.get)
    incumbent = authority.active_lease("owner")
    assert incumbent is not None
    holder = wake.register_lease_holder(
        root,
        controller_label="owner",
        lease_nonce=incumbent.nonce,
    )
    code = dispatch.main(
        [
            "--agent", "codex",
            "--prompt", "must never launch",
            "--cwd", str(root),
            "--controller-label", "owner",
            "--controller-beacon-pid", "62002",
        ]
    )
    holder.close()
    assert code == 73
    error = capsys.readouterr().err
    assert "label in use" in error
    assert "goalflight_dispatch.py" in error
    assert "--takeover" in error


def test_dispatch_help_exposes_takeover_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        dispatch.main(["--help"])
    assert exit_info.value.code == 0
    assert "--takeover" in capsys.readouterr().out


def test_queue_and_detached_children_verify_without_claim_or_renew(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority = _state(monkeypatch, tmp_path)
    result = authority.claim_or_renew_lease(
        "owner", principal={"pid": 62001, "start_token": "incumbent"}
    )
    assert result.committed and result.value is not None
    before = result.value
    holder = wake.register_lease_holder(
        root,
        controller_label="owner",
        lease_nonce=before.nonce,
    )
    monkeypatch.setattr(
        sessions,
        "_controller_process_identity",
        lambda pid: {"pid": pid, "start_token": "incumbent"},
    )
    child = _args(from_queue=True, controller_session_id=before.nonce)
    stamped = dispatch._stamp_controller_session(child, root)
    assert stamped["reason"] == "role_does_not_claim"
    assert child.controller_session_id == before.nonce
    after = authority.active_lease("owner")
    assert after is not None
    assert after.renewed_at == before.renewed_at
    assert after.renew_deadline_at == before.renew_deadline_at

    missing_nonce = _args(from_queue=True, controller_session_id=None)
    dispatch._stamp_controller_session(missing_nonce, root)
    assert missing_nonce.controller_session_id is None
    assert missing_nonce.controller_label is None

    audit_identity_changed = _args(from_queue=True, controller_session_id=before.nonce)
    monkeypatch.setattr(
        sessions,
        "_controller_process_identity",
        lambda pid: {"pid": pid, "start_token": "reused"},
    )
    dispatch._stamp_controller_session(audit_identity_changed, root)
    assert audit_identity_changed.controller_session_id == before.nonce
    assert audit_identity_changed.controller_label == "owner"
    holder.close()
