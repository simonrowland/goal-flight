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


def _state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, journal.Journal]:
    for key, value in {
        "GOALFLIGHT_TASK_STORE_DIR": tmp_path / "task-store",
        "GOALFLIGHT_JOURNAL_DIR": tmp_path / "journal",
        "GOALFLIGHT_MESSAGES_DIR": tmp_path / "messages",
        "GOALFLIGHT_STATE_DIR": tmp_path / "state",
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
    }
    values.update(overrides)
    return SimpleNamespace(**values)


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


def test_expired_controller_lease_is_takeable_by_a_new_incarnation(
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
    claimed = authority.claim_or_renew_lease(
        "owner", principal={"pid": 62002, "start_token": "successor"}
    )
    assert claimed.committed and claimed.value is not None
    successor = authority.active_lease("owner")
    assert successor is not None
    assert successor.generation == incumbent.value.generation + 1
    assert successor.nonce != incumbent.value.nonce
    assert successor.principal["pid"] == 62002


def test_dispatch_auto_claim_conflict_is_visible_and_never_steals(
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
        lambda pid: {"pid": pid, "start_token": "different"},
    )
    args = _args(controller_beacon_pid=62002)
    result = dispatch._stamp_controller_session(args, root)
    assert result["reason"] == "label_in_use"
    assert "label in use" in str(result["message"])
    assert args.controller_label is None and args.controller_session_id is None
    assert authority.active_lease("owner") == incumbent.value


def test_dispatch_main_returns_visible_label_in_use_before_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, authority = _state(monkeypatch, tmp_path)
    assert authority.claim_or_renew_lease(
        "owner", principal={"pid": 62001, "start_token": "incumbent"}
    ).committed
    monkeypatch.setattr(
        sessions,
        "_controller_process_identity",
        lambda pid: {"pid": pid, "start_token": "different"},
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
    assert code == 73
    assert "label in use" in capsys.readouterr().err


def test_queue_and_detached_children_verify_without_claim_or_renew(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority = _state(monkeypatch, tmp_path)
    result = authority.claim_or_renew_lease(
        "owner", principal={"pid": 62001, "start_token": "incumbent"}
    )
    assert result.committed and result.value is not None
    before = result.value
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

    stale_identity = _args(from_queue=True, controller_session_id=before.nonce)
    monkeypatch.setattr(
        sessions,
        "_controller_process_identity",
        lambda pid: {"pid": pid, "start_token": "reused"},
    )
    dispatch._stamp_controller_session(stale_identity, root)
    assert stale_identity.controller_session_id is None
    assert stale_identity.controller_label is None
