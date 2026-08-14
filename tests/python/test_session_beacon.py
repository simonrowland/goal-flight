"""PID/start-token principals are verified within journal lease generations."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_session_status as sessions  # noqa: E402


def _root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE_DIR", str(tmp_path / "task-store"))
    monkeypatch.setenv("GOALFLIGHT_JOURNAL_DIR", str(tmp_path / "journal"))
    root = tmp_path / "project"
    root.mkdir()
    return root


def test_same_measured_process_generation_renews_idempotently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _root(monkeypatch, tmp_path)
    monkeypatch.setattr(
        sessions,
        "_controller_process_identity",
        lambda pid: {"pid": pid, "start_token": "same-generation"},
    )
    first = sessions.claim_controller_startup(
        root, pid=71001, label="controller", role="controller"
    )
    second = sessions.claim_controller_startup(
        root, pid=71001, label="controller", role="controller"
    )
    assert first["claimed"] is True and second["claimed"] is True
    assert second["session"]["id"] == first["session"]["id"]
    assert second["session"]["generation"] == first["session"]["generation"]


def test_reused_pid_generation_cannot_take_live_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _root(monkeypatch, tmp_path)
    tokens = {71001: "first-generation"}
    monkeypatch.setattr(
        sessions,
        "_controller_process_identity",
        lambda pid: {"pid": pid, "start_token": tokens[pid]},
    )
    first = sessions.claim_session(root, pid=71001, label="controller")
    tokens[71001] = "reused-generation"
    result = sessions.claim_controller_startup(
        root,
        pid=71001,
        label="controller",
        role="controller",
        session_id=first["id"],
    )
    assert result["reason"] == "label_in_use"
    assert journal.Journal(root).active_lease("controller").nonce == first["id"]


def test_release_requires_exact_process_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _root(monkeypatch, tmp_path)
    token = {"value": "generation-a"}
    monkeypatch.setattr(
        sessions,
        "_controller_process_identity",
        lambda pid: {"pid": pid, "start_token": token["value"]},
    )
    sessions.claim_session(root, pid=71001, label="controller")
    token["value"] = "generation-b"
    assert sessions.release_session(root, pid=71001) is False
    token["value"] = "generation-a"
    assert sessions.release_session(root, pid=71001) is True
    assert journal.Journal(root).active_lease("controller") is None
